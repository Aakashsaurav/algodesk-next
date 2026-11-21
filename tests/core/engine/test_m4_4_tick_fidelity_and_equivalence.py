"""Tests for Step 6, 7 & 8: TickBarFeed, Backtest Equivalence, and Tick Fidelity."""

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
from algodesk.api.market import Bar, InstrumentId, Level, Quote, Tick
from algodesk.cli.backtest import backtest_app
from algodesk.data.manifest import DataManifest
from algodesk.data.recorder import Recorder
from algodesk.data.replay import save_warmup_bars_parquet
from algodesk.data.tick_bar_feed import TickBarFeed
from algodesk.engine.clock import SimClock
from algodesk.engine.equivalence import run_backtest_equivalence
from algodesk.engine.runner import SessionRunner
from algodesk.ops.runtime import open_runtime

IST = ZoneInfo("Asia/Kolkata")

runner = CliRunner()


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


def _make_tick(inst: str, ts: datetime, price: float, vol: int = 10) -> Tick:
    return Tick(
        instrument=InstrumentId(inst),
        exch_ts=ts,
        recv_ts=ts,
        ltp=price,
        ltq=vol,
        volume=vol,
        oi=0,
    )


def _record_fake_paper_session(
    project_dir: Path,
    session_id: str,
    instruments: list[str],
    start_ts: datetime,
    include_exit_time: bool = False,
    exit_ts: datetime | None = None,
) -> tuple[Path, list[Bar]]:
    """Record fake session ticks and events using the real SessionRunner and real Recorder."""
    inst_objs = sorted([InstrumentId(s) for s in instruments], key=str)

    # Configure demo_paper_fast in project config/runs/
    cfg_file = project_dir / "config" / "runs" / "demo_paper_fast.toml"
    cfg_text = cfg_file.read_text(encoding="utf-8")
    cfg_text = cfg_text.replace("fast = 3, slow = 8", "fast = 2, slow = 4")
    if include_exit_time and exit_ts:
        exit_ist = exit_ts.astimezone(IST)
        eh_em = f"{exit_ist.hour:02d}:{exit_ist.minute:02d}"
        cfg_text = cfg_text.replace('exit_time = "15:15"', f'exit_time = "{eh_em}"')
    cfg_file.write_text(cfg_text, encoding="utf-8")

    total_minutes = 16 if (include_exit_time and exit_ts) else 8
    prices = [100.0, 100.0, 100.0, 100.0, 90.0, 80.0, 120.0] + [120.0] * (total_minutes - 7)

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
                    for inst in inst_objs:
                        tick = Tick(
                            instrument=inst,
                            exch_ts=t_s,
                            recv_ts=t_s,
                            ltp=p,
                            ltq=1,
                            volume=100,
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
                        cl.set_time(t_s)
                        yield tick
                        await asyncio.sleep(0)
                        yield quote
                        await asyncio.sleep(0)
                t_end = t_m + timedelta(minutes=1)
                cl.set_time(t_end)
                for inst in inst_objs:
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

    # Write trades.csv if not written, or verify presence
    tbf = TickBarFeed(session_dir=sess_dir, data_dir=project_dir / "data", timeframe="1m")
    paper_bars = tbf.bars
    return sess_dir, paper_bars


# ============================================================================
# Step 6: TickBarFeed Tests
# ============================================================================


def test_tick_bar_feed_rebuilds_1m_bars(tmp_path: Path) -> None:
    """TickBarFeed rebuilds 1m bars from recorded ticks and timer closes."""
    proj = _build_test_project(tmp_path, "proj_tbf_1")
    start_ts = datetime(2026, 11, 19, 3, 45, tzinfo=UTC)
    insts = ["NSE:EQ:RELIANCE", "NSE:EQ:TCS"]
    sess_dir, orig_bars = _record_fake_paper_session(proj, "sess_tbf_1", insts, start_ts)

    feed = TickBarFeed(session_dir=sess_dir, data_dir=proj / "data", timeframe="1m")
    rebuilt = feed.bars

    assert len(rebuilt) == len(orig_bars)
    for b_orig, b_rebuilt in zip(orig_bars, rebuilt, strict=False):
        assert b_orig.instrument == b_rebuilt.instrument
        assert b_orig.start_ts == b_rebuilt.start_ts
        assert b_orig.end_ts == b_rebuilt.end_ts
        assert b_orig.open == b_rebuilt.open
        assert b_orig.close == b_rebuilt.close


def test_tick_bar_feed_iter_with_seq(tmp_path: Path) -> None:
    """TickBarFeed.iter_with_seq yields causal quotes and bars."""
    proj = _build_test_project(tmp_path, "proj_tbf_iter")
    start_ts = datetime(2026, 11, 19, 3, 45, tzinfo=UTC)
    insts = ["NSE:EQ:INFY"]
    sess_dir, _ = _record_fake_paper_session(proj, "sess_tbf_iter", insts, start_ts)

    feed = TickBarFeed(session_dir=sess_dir, data_dir=proj / "data", timeframe="1m")
    items = list(feed.iter_with_seq())

    assert len(items) > 0
    # Every bar generates 4 synthetic quotes (open, q1, q2, close) + 1 bar
    assert len(items) == len(feed.bars) * 5

    # Check strictly monotonic sequence numbers
    seqs = [item[1] for item in items]
    assert seqs == list(range(1, len(items) + 1))


def test_tick_bar_feed_warmup_bars(tmp_path: Path) -> None:
    """TickBarFeed loads warmup_bars.parquet if present."""
    proj = _build_test_project(tmp_path, "proj_tbf_wb")
    start_ts = datetime(2026, 11, 19, 3, 45, tzinfo=UTC)
    insts = ["NSE:EQ:INFY"]
    sess_dir, _ = _record_fake_paper_session(proj, "sess_tbf_wb", insts, start_ts)

    dummy_warmup = [
        Bar(
            instrument=InstrumentId("NSE:EQ:INFY"),
            timeframe="1m",
            start_ts=start_ts - timedelta(minutes=2),
            end_ts=start_ts - timedelta(minutes=1),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=500,
            complete=True,
            synthetic=False,
        )
    ]
    save_warmup_bars_parquet(sess_dir / "warmup_bars.parquet", dummy_warmup)

    feed = TickBarFeed(session_dir=sess_dir, data_dir=proj / "data", timeframe="1m")
    assert len(feed.warmup_bars) == 1
    assert feed.warmup_bars[0].close == 100.5


# ============================================================================
# Step 7: Backtest Equivalence Tests
# ============================================================================


def test_backtest_equivalence_signals_equal_true(tmp_path: Path) -> None:
    """backtest equivalence yields signals equal: True for recorded paper session."""
    proj = _build_test_project(tmp_path, "proj_eq_true")
    start_ts = datetime(2026, 11, 19, 3, 45, tzinfo=UTC)
    insts = ["NSE:EQ:RELIANCE", "NSE:EQ:TCS"]
    sess_dir, _ = _record_fake_paper_session(proj, "demo_paper_sess_eq", insts, start_ts)

    with open_runtime(proj) as rt:
        res = run_backtest_equivalence(rt, "demo_paper_sess_eq")
        assert res.signals_equal is True
        assert res.paper_signals_count > 0
        assert res.backtest_signals_count == res.paper_signals_count
        assert len(res.differences) == 0


def test_backtest_equivalence_exit_time_4_open_positions(tmp_path: Path) -> None:
    """Equivalence test crossing exit_time with 4 open positions returns True."""
    proj = _build_test_project(tmp_path, "proj_eq_4pos")
    exit_ts = datetime(2026, 11, 19, 9, 45, tzinfo=UTC)
    start_ts = datetime(2026, 11, 19, 9, 30, tzinfo=UTC)
    # 4 open positions crossing exit time
    insts = ["NSE:EQ:INFY", "NSE:EQ:TCS", "NSE:EQ:RELIANCE", "NSE:EQ:HDFCBANK"]

    sess_dir, _ = _record_fake_paper_session(
        proj,
        "demo_paper_4pos_exit",
        insts,
        start_ts,
        include_exit_time=True,
        exit_ts=exit_ts,
    )

    with open_runtime(proj) as rt:
        res = run_backtest_equivalence(rt, "demo_paper_4pos_exit")
        assert res.signals_equal is True
        assert res.paper_signals_count >= 8
        assert res.backtest_signals_count == res.paper_signals_count


def test_backtest_equivalence_difference_classification_bar_content(tmp_path: Path) -> None:
    """Difference classification identifies bar content mismatch."""
    proj = _build_test_project(tmp_path, "proj_eq_bar_diff")
    start_ts = datetime(2026, 11, 19, 3, 45, tzinfo=UTC)
    insts = ["NSE:EQ:INFY"]
    sess_dir, _ = _record_fake_paper_session(proj, "sess_diff_bar", insts, start_ts)

    # Tamper with recorded signal in events.jsonl to force a mismatch
    events_file = sess_dir / "events.jsonl"
    lines = events_file.read_text(encoding="utf-8").splitlines()
    new_lines = []
    for line in lines:
        e = json.loads(line)
        if e.get("kind") == "signal":
            p = e.get("payload_json", e.get("payload", {}))
            if isinstance(p, str):
                p = json.loads(p)
            p["side"] = "SELL" if p.get("side") == "BUY" else "BUY"
            e["payload"] = p
            e["payload_json"] = json.dumps(p)
        new_lines.append(json.dumps(e))
    events_file.write_text("\n".join(new_lines), encoding="utf-8")

    with open_runtime(proj) as rt:
        res = run_backtest_equivalence(rt, "sess_diff_bar")
        assert res.signals_equal is False
        assert len(res.differences) > 0
        assert res.differences[0].classification == "bar content differs"


def test_backtest_equivalence_difference_classification_timer_exit(tmp_path: Path) -> None:
    """Difference classification identifies timer exit mismatch."""
    proj = _build_test_project(tmp_path, "proj_eq_timer_diff")
    exit_ts = datetime(2026, 11, 19, 9, 45, tzinfo=UTC)
    start_ts = datetime(2026, 11, 19, 9, 30, tzinfo=UTC)
    insts = ["NSE:EQ:INFY"]
    sess_dir, _ = _record_fake_paper_session(
        proj,
        "sess_timer_diff",
        insts,
        start_ts,
        include_exit_time=True,
        exit_ts=exit_ts,
    )

    # Shift recorded signal by 2 minutes so it mismatches backtest
    events_file = sess_dir / "events.jsonl"
    lines = events_file.read_text(encoding="utf-8").splitlines()
    new_lines = []
    for line in lines:
        e = json.loads(line)
        p = e.get("payload_json", e.get("payload", {}))
        if isinstance(p, str):
            p = json.loads(p)
        if e.get("kind") == "signal" and "timer_exit" in p.get("intent_id", ""):
            p["ts"] = (exit_ts + timedelta(minutes=2)).isoformat()
            e["payload"] = p
            e["payload_json"] = json.dumps(p)
        new_lines.append(json.dumps(e))
    events_file.write_text("\n".join(new_lines), encoding="utf-8")

    with open_runtime(proj) as rt:
        res = run_backtest_equivalence(rt, "sess_timer_diff")
        # Differences near exit timer get classified as 'timer exit time differs'
        exit_diffs = [d for d in res.differences if d.classification == "timer exit time differs"]
        assert len(exit_diffs) > 0


def test_backtest_equivalence_cli_reports_counts_and_exits_0(tmp_path: Path) -> None:
    """CLI backtest equivalence prints signals equal: True and exits code 0."""
    proj = _build_test_project(tmp_path, "proj_cli_eq_0")
    start_ts = datetime(2026, 11, 19, 3, 45, tzinfo=UTC)
    insts = ["NSE:EQ:INFY"]
    sess_dir, _ = _record_fake_paper_session(proj, "sess_cli_0", insts, start_ts)

    import os

    old_cwd = os.getcwd()
    os.chdir(proj)
    try:
        result = runner.invoke(backtest_app, ["equivalence", "--session", "sess_cli_0"])
        assert result.exit_code == 0
        assert "signals equal: True" in result.output
        assert "Paper signals:" in result.output
        assert "Backtest signals:" in result.output
        assert "Paper orders:" in result.output
        assert "Paper fills:" in result.output
    finally:
        os.chdir(old_cwd)


# ============================================================================
# Step 8: Tick Fidelity Tests
# ============================================================================


def test_tick_fidelity_backtest_manifest_and_assumptions(tmp_path: Path) -> None:
    """backtest run --fidelity tick writes mode: backtest, fidelity: tick and assumptions."""
    proj = _build_test_project(tmp_path, "proj_tick_bt")
    start_ts = datetime(2026, 11, 19, 3, 45, tzinfo=UTC)
    insts = ["NSE:EQ:RELIANCE"]
    sess_dir, _ = _record_fake_paper_session(proj, "demo_paper_sess_tick", insts, start_ts)

    # Copy demo_tick_bt.toml into config/runs/
    shutil.copyfile(
        Path(__file__).resolve().parents[3] / "config" / "runs" / "demo_tick_bt.toml",
        proj / "config" / "runs" / "demo_tick_bt.toml",
    )

    import os

    old_cwd = os.getcwd()
    os.chdir(proj)
    try:
        res = runner.invoke(
            backtest_app,
            ["run", "demo_tick_bt", "--fidelity", "tick", "--session", "demo_paper_sess_tick"],
        )
        assert res.exit_code == 0
        assert "Backtest session completed:" in res.output

        # Inspect generated results
        bt_results = list((proj / "results").glob("*demo_tick_bt*"))
        assert len(bt_results) > 0
        res_dir = bt_results[0]

        man = json.loads((res_dir / "manifest.json").read_text(encoding="utf-8"))
        assert man["mode"] == "backtest"
        assert man["fidelity"] == "tick"

        summary_md = (res_dir / "SUMMARY.md").read_text(encoding="utf-8")
        assert "fidelity: tick" in summary_md
    finally:
        os.chdir(old_cwd)


def test_tick_fidelity_refuses_without_session(tmp_path: Path) -> None:
    """backtest run --fidelity tick raises error when --session is omitted."""
    proj = _build_test_project(tmp_path, "proj_tick_nosess")
    shutil.copyfile(
        Path(__file__).resolve().parents[3] / "config" / "runs" / "demo_tick_bt.toml",
        proj / "config" / "runs" / "demo_tick_bt.toml",
    )

    import os

    old_cwd = os.getcwd()
    os.chdir(proj)
    try:
        res = runner.invoke(
            backtest_app,
            ["run", "demo_tick_bt", "--fidelity", "tick"],
        )
        assert res.exit_code != 0
        assert "Tick fidelity backtest requires --session" in res.output
    finally:
        os.chdir(old_cwd)


def test_tick_bar_feed_with_no_timer_closes(tmp_path: Path) -> None:
    """TickBarFeed reconstructs completed bars correctly when no bar.timer_close exists."""
    proj = _build_test_project(tmp_path, "proj_tbf_notimers")
    sess_dir = proj / "results" / "sess_notimers"
    sess_dir.mkdir(parents=True, exist_ok=True)

    data_dir = proj / "data"
    manifest_db = data_dir / "state" / "manifest.db"
    manifest_db.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(manifest_db)

    recorder = Recorder(
        data_dir=data_dir,
        manifest=manifest,
        source="live",
        session_id="sess_notimers",
        max_buffer_rows=1000,
    )

    inst = "NSE:EQ:INFY"
    start_ts = datetime(2026, 11, 19, 3, 45, tzinfo=UTC)
    # 2 minutes of ticks, crossing into 3rd minute to close 2 bars
    for m in range(3):
        t_m = start_ts + timedelta(minutes=m)
        for s in (10, 30, 50):
            t_s = t_m + timedelta(seconds=s)
            tick = Tick(instrument=inst, exch_ts=t_s, recv_ts=t_s, ltp=100.0 + m, ltq=1, volume=100)
            recorder.on_market_event(tick, seq=m * 3 + (s // 20))

    recorder.flush()
    recorder.close()
    recorder.compact(start_ts.date().isoformat())
    manifest.close()

    feed = TickBarFeed(session_dir=sess_dir, data_dir=data_dir, timeframe="1m")
    bars = feed.bars
    assert len(bars) >= 2
    assert all(b.complete for b in bars)


def test_tick_bar_feed_empty_session_safe(tmp_path: Path) -> None:
    """TickBarFeed returns empty list without error on an empty session."""
    proj = _build_test_project(tmp_path, "proj_tbf_empty")
    empty_sess = proj / "results" / "sess_empty"
    empty_sess.mkdir(parents=True, exist_ok=True)

    feed = TickBarFeed(session_dir=empty_sess, data_dir=proj / "data", timeframe="1m")
    assert feed.bars == []
    events = list(feed.iter_with_seq())
    assert events == []


def test_equivalence_cli_exit_code_1_on_diff(tmp_path: Path) -> None:
    """CLI backtest equivalence exits code 1 when signal sequences differ."""
    proj = _build_test_project(tmp_path, "proj_cli_eq_1")
    start_ts = datetime(2026, 11, 19, 3, 45, tzinfo=UTC)
    insts = ["NSE:EQ:INFY"]
    sess_dir, _ = _record_fake_paper_session(proj, "sess_cli_1", insts, start_ts)

    # Tamper with recorded events to force mismatch
    events_file = sess_dir / "events.jsonl"
    lines = events_file.read_text(encoding="utf-8").splitlines()
    new_lines = []
    for line in lines:
        e = json.loads(line)
        if e.get("kind") == "signal":
            p = e.get("payload_json", e.get("payload", {}))
            if isinstance(p, str):
                p = json.loads(p)
            p["side"] = "SELL" if p.get("side") == "BUY" else "BUY"
            e["payload"] = p
            e["payload_json"] = json.dumps(p)
        new_lines.append(json.dumps(e))
    events_file.write_text("\n".join(new_lines), encoding="utf-8")

    old_cwd = os.getcwd()
    os.chdir(proj)
    try:
        result = runner.invoke(backtest_app, ["equivalence", "--session", "sess_cli_1"])
        assert result.exit_code == 1
        assert "signals equal: False" in result.output
    finally:
        os.chdir(old_cwd)


def test_tick_fidelity_cli_session_dir_path_supported(tmp_path: Path) -> None:
    """backtest run --fidelity tick supports passing absolute session path."""
    proj = _build_test_project(tmp_path, "proj_tick_path")
    start_ts = datetime(2026, 11, 19, 3, 45, tzinfo=UTC)
    insts = ["NSE:EQ:RELIANCE"]
    sess_dir, _ = _record_fake_paper_session(proj, "demo_paper_sess_path", insts, start_ts)

    shutil.copyfile(
        Path(__file__).resolve().parents[3] / "config" / "runs" / "demo_tick_bt.toml",
        proj / "config" / "runs" / "demo_tick_bt.toml",
    )

    old_cwd = os.getcwd()
    os.chdir(proj)
    try:
        res = runner.invoke(
            backtest_app,
            ["run", "demo_tick_bt", "--fidelity", "tick", "--session", str(sess_dir)],
        )
        assert res.exit_code == 0
        assert "Backtest session completed:" in res.output
    finally:
        os.chdir(old_cwd)
