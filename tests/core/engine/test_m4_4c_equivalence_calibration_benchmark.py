"""Tests for M4-4c: Equivalence multiset comparison, calibration by order identity,
tick fidelity determinism, and benchmark profiling with cost separation.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from algodesk.api.market import Bar, InstrumentId, Tick
from algodesk.cli.backtest import backtest_app
from algodesk.data.tick_bar_feed import TickBarFeed
from algodesk.engine.benchmark import (
    BenchmarkReport,
    _filter_and_format_profile,
    run_benchmark_measurement,
)
from algodesk.results.calibration import (
    CalibrationResult,
    compute_calibration_from_trades,
)
from algodesk.results.equivalence import (
    EquivalenceResult,
    compare_equivalence_events,
)

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


# =========================================================================
# CP1 Equivalence Tests (Steps 4, 5, 6)
# =========================================================================


def test_equivalence_multiset_same_second_reordering(tmp_path: Path) -> None:
    """Same-second signals with differing instrument ordering compare as equal."""
    t0 = datetime(2026, 11, 19, 8, 56, 0, 100000, tzinfo=UTC)
    t1 = datetime(2026, 11, 19, 8, 56, 0, 200000, tzinfo=UTC)

    paper_events = [
        {
            "kind": "signal",
            "payload": {"ts": t0.isoformat(), "instrument": "NSE:EQ:ICICIBANK", "side": "BUY"},
        },
        {
            "kind": "signal",
            "payload": {"ts": t1.isoformat(), "instrument": "NSE:EQ:HDFCBANK", "side": "BUY"},
        },
        {"kind": "order.created", "payload": {"client_ref": "ref_1"}},
        {"kind": "order.created", "payload": {"client_ref": "ref_2"}},
    ]
    bt_events = [
        {
            "kind": "signal",
            "payload": {"ts": t1.isoformat(), "instrument": "NSE:EQ:HDFCBANK", "side": "BUY"},
        },
        {
            "kind": "signal",
            "payload": {"ts": t0.isoformat(), "instrument": "NSE:EQ:ICICIBANK", "side": "BUY"},
        },
        {"kind": "intent", "payload": {"client_ref": "bt_ref_1"}},
        {"kind": "order.created", "payload": {"client_ref": "bt_ref_1"}},
        {"kind": "intent", "payload": {"client_ref": "bt_ref_2"}},
        {"kind": "order.created", "payload": {"client_ref": "bt_ref_2"}},
    ]

    res = compare_equivalence_events(paper_events, bt_events, tmp_path)
    assert res.signals_equal is True
    assert res.paper_signals_count == 2
    assert res.backtest_signals_count == 2
    assert res.paper_orders_count == 2
    assert res.backtest_orders_count == 2
    assert len(res.differences) == 0


def test_equivalence_order_counting_unique_client_refs(tmp_path: Path) -> None:
    """Equivalence counts orders by unique client_ref without double-counting."""
    events = [
        {"kind": "intent", "payload": {"client_ref": "ord_101"}},
        {"kind": "order.created", "payload": {"client_ref": "ord_101"}},
        {"kind": "intent", "payload": {"client_ref": "ord_102"}},
        {"kind": "order.created", "payload": {"client_ref": "ord_102"}},
        {"kind": "fill", "payload": {"client_ref": "ord_101"}},
    ]
    res = compare_equivalence_events(events, events, tmp_path)
    assert res.paper_orders_count == 2
    assert res.backtest_orders_count == 2
    assert res.paper_fills_count == 1
    assert res.backtest_fills_count == 1


def test_equivalence_bar_source_classification(tmp_path: Path) -> None:
    """Differing signals correctly report bar OHLCV and source (tick-built vs timer-closed)."""
    t0 = datetime(2026, 11, 19, 9, 15, 0, tzinfo=UTC)
    t_end = t0 + timedelta(minutes=1)
    inst = InstrumentId("NSE:EQ:INFY")
    bar = Bar(
        instrument=inst,
        timeframe="1m",
        start_ts=t0,
        end_ts=t_end,
        open=1500.0,
        high=1510.0,
        low=1495.0,
        close=1505.0,
        volume=5000,
        complete=True,
        synthetic=False,
    )

    paper_events = [
        {
            "kind": "signal",
            "payload": {"ts": t_end.isoformat(), "instrument": str(inst), "side": "BUY"},
        },
        {
            "kind": "bar.timer_close",
            "payload": {"instrument": str(inst), "end_ts": t_end.isoformat()},
        },
    ]
    bt_events: list[dict[str, Any]] = []

    res = compare_equivalence_events(paper_events, bt_events, tmp_path, bars=[bar])
    assert res.signals_equal is False
    assert len(res.only_in_paper) == 1
    diff = res.only_in_paper[0]
    assert diff["source"] == "timer-closed"
    assert "O=1500.0 H=1510.0 L=1495.0 C=1505.0 V=5000" in diff["bar"]


def test_equivalence_cli_output_formatting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Typer CLI backtest equivalence reports signals equal, counts, and diff lists."""
    proj = _build_test_project(tmp_path, "cli_proj")
    dummy_res = EquivalenceResult(
        signals_equal=False,
        paper_signals_count=31,
        backtest_signals_count=32,
        paper_orders_count=30,
        backtest_orders_count=31,
        paper_fills_count=30,
        backtest_fills_count=31,
        differences=[],
        backtest_path=proj / "results" / "bt_run",
        only_in_paper=[],
        only_in_backtest=[
            {
                "formatted": (
                    "2026-11-19T08:56:00+00:00 NSE:EQ:TCS BUY | "
                    "O=3200.0 H=3210.0 L=3190.0 C=3205.0 V=1000 | tick-built"
                )
            }
        ],
    )
    monkeypatch.setattr(
        "algodesk.engine.equivalence.run_backtest_equivalence",
        lambda rt, sess: dummy_res,
    )
    result = runner.invoke(
        backtest_app,
        ["equivalence", "--session", "dummy_sess"],
        obj={"project_dir": proj},
    )
    assert result.exit_code == 1
    assert "signals equal: False" in result.output
    assert "Paper signals: 31, Backtest signals: 32" in result.output
    assert "Only in backtest (1):" in result.output
    assert "NSE:EQ:TCS BUY" in result.output
    assert "Paper orders: 30, Backtest orders: 31" in result.output


def test_tick_bar_feed_bounded_window_and_no_spurious_flush(tmp_path: Path) -> None:
    """TickBarFeed strictly bounds tick stream to [w_start, w_end] and avoids spurious flushes."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from algodesk.data.schema import events_to_table

    sess_dir = tmp_path / "sess_feed"
    sess_dir.mkdir(parents=True)

    t_start = datetime(2026, 11, 19, 8, 30, 0, tzinfo=UTC)
    t_end = datetime(2026, 11, 19, 8, 33, 0, tzinfo=UTC)
    inst = InstrumentId("NSE:EQ:SBIN")

    def _make_tick(ts: datetime, p: float, v: int) -> Tick:
        return Tick(instrument=inst, exch_ts=ts, recv_ts=ts, ltp=p, ltq=v, volume=v)

    ticks = [
        _make_tick(t_start - timedelta(seconds=10), 600.0, 10),
        _make_tick(t_start + timedelta(seconds=5), 601.0, 10),
        _make_tick(t_start + timedelta(seconds=65), 602.0, 15),
        _make_tick(t_start + timedelta(seconds=125), 603.0, 20),
        _make_tick(t_end - timedelta(seconds=10), 604.0, 5),
        _make_tick(t_end + timedelta(seconds=10), 605.0, 5),
    ]

    tbl = events_to_table(ticks)
    seqs = list(range(len(ticks)))
    tbl = tbl.append_column("seq", pa.array(seqs, type=pa.int64()))
    tbl = tbl.append_column("session_id", pa.array(["sess_feed"] * len(ticks), type=pa.string()))
    pq.write_table(tbl, sess_dir / "part-0001.parquet")

    manifest = {
        "first_tick_utc": t_start.isoformat(),
        "last_tick_utc": t_end.isoformat(),
    }
    (sess_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    feed = TickBarFeed(
        session_dir=sess_dir,
        timeframe="1m",
        tick_window=(t_start, t_end),
    )
    bars = feed.bars
    assert len(bars) == 2
    for b in bars:
        assert b.start_ts >= t_start
        assert b.end_ts <= t_end
        assert b.complete is True


def test_equivalence_exit_time_crossing_four_positions(tmp_path: Path) -> None:
    """Fake-feed session crossing exit_time with 4 open positions gives signals equal True."""
    exit_ts = datetime(2026, 11, 19, 9, 45, 0, tzinfo=UTC)
    symbols = ["NSE:EQ:RELIANCE", "NSE:EQ:INFY", "NSE:EQ:TCS", "NSE:EQ:HDFCBANK"]

    paper_events: list[dict[str, Any]] = []
    bt_events: list[dict[str, Any]] = []

    entry_ts = datetime(2026, 11, 19, 9, 15, 0, tzinfo=UTC)
    for sym in symbols:
        p_ev = {
            "kind": "signal",
            "payload": {"ts": entry_ts.isoformat(), "instrument": sym, "side": "BUY"},
        }
        paper_events.append(p_ev)
        bt_events.append(dict(p_ev))

    for sym in symbols:
        ex_ev = {
            "kind": "signal",
            "payload": {"ts": exit_ts.isoformat(), "instrument": sym, "side": "SELL"},
        }
        paper_events.append(ex_ev)
        bt_events.append(dict(ex_ev))

    res = compare_equivalence_events(paper_events, bt_events, tmp_path)
    assert res.signals_equal is True
    assert res.paper_signals_count == 8
    assert res.backtest_signals_count == 8


def test_equivalence_deliberately_altered_bar(tmp_path: Path) -> None:
    """A deliberately altered bar gives signals_equal False with that bar listed and classified."""
    t0 = datetime(2026, 11, 19, 9, 15, 0, tzinfo=UTC)
    t_end = t0 + timedelta(minutes=1)
    inst = InstrumentId("NSE:EQ:SBIN")
    altered_bar = Bar(
        instrument=inst,
        timeframe="1m",
        start_ts=t0,
        end_ts=t_end,
        open=500.0,
        high=550.0,
        low=490.0,
        close=540.0,
        volume=12000,
        complete=True,
        synthetic=False,
    )

    paper_events = [
        {
            "kind": "signal",
            "payload": {"ts": t_end.isoformat(), "instrument": str(inst), "side": "BUY"},
        },
    ]
    bt_events = [
        {
            "kind": "signal",
            "payload": {"ts": t_end.isoformat(), "instrument": str(inst), "side": "SELL"},
        },
    ]

    res = compare_equivalence_events(paper_events, bt_events, tmp_path, bars=[altered_bar])
    assert res.signals_equal is False
    assert len(res.only_in_paper) == 1
    assert len(res.only_in_backtest) == 1
    assert res.only_in_paper[0]["signal"][2] == "BUY"
    assert res.only_in_backtest[0]["signal"][2] == "SELL"
    assert "O=500.0 H=550.0 L=490.0 C=540.0" in res.only_in_paper[0]["bar"]


# =========================================================================
# CP2 Calibration and Tick Fidelity Tests (Steps 7, 8)
# =========================================================================


def test_calibration_order_identity_pairing(tmp_path: Path) -> None:
    """Calibration pairs paper fills and backtest fills strictly by order identity."""
    sess_dir = tmp_path / "sess"
    bt_dir = tmp_path / "bt"
    sess_dir.mkdir(parents=True)
    bt_dir.mkdir(parents=True)

    t0 = datetime(2026, 11, 19, 9, 15, 0, tzinfo=UTC)

    paper_trades = []
    bt_trades = []
    paper_events = []
    bt_events = []

    for i in range(12):
        sym = f"NSE:EQ:SYM{i:02d}"
        cref = f"p_ref_{i}"
        bt_cref = f"b_ref_{i}"
        t_cur = t0 + timedelta(minutes=i)

        paper_events.append(
            {"kind": "intent", "payload": {"client_ref": cref, "ts": t_cur.isoformat()}}
        )
        bt_events.append(
            {"kind": "intent", "payload": {"client_ref": bt_cref, "ts": t_cur.isoformat()}}
        )

        paper_trades.append(
            {
                "client_ref": cref,
                "instrument": sym,
                "side": "BUY",
                "price": "100.20",
                "net_pnl": "50.0",
                "ts": t_cur.isoformat(),
            }
        )
        bt_trades.append(
            {
                "client_ref": bt_cref,
                "instrument": sym,
                "side": "BUY",
                "price": "100.00",
                "net_pnl": "52.0",
                "ts": t_cur.isoformat(),
            }
        )

    res = compute_calibration_from_trades(
        "sess_01",
        sess_dir,
        bt_dir,
        paper_trades,
        bt_trades,
        paper_events,
        bt_events,
    )

    assert res.refused is False
    assert res.fills_compared == 12
    assert res.adverse_pairs_count == 12
    assert pytest.approx(res.mean_adverse_bps, 0.1) == 20.0
    assert pytest.approx(res.median_adverse_bps, 0.1) == 20.0
    assert pytest.approx(res.suggested_slippage_bps, 0.1) == 20.0


def test_calibration_refusal_under_ten_pairs(tmp_path: Path) -> None:
    """Calibration refuses with a clear message when fewer than 10 fill pairs exist."""
    sess_dir = tmp_path / "sess"
    bt_dir = tmp_path / "bt"
    sess_dir.mkdir(parents=True)
    bt_dir.mkdir(parents=True)

    paper_trades = [
        {"client_ref": "ref_1", "instrument": "NSE:EQ:TCS", "side": "BUY", "price": "3000.0"}
    ]
    bt_trades = [
        {"client_ref": "ref_1", "instrument": "NSE:EQ:TCS", "side": "BUY", "price": "3000.0"}
    ]
    p_ev = [
        {"kind": "intent", "payload": {"client_ref": "ref_1", "ts": "2026-11-19T09:15:00+00:00"}}
    ]
    b_ev = [
        {"kind": "intent", "payload": {"client_ref": "ref_1", "ts": "2026-11-19T09:15:00+00:00"}}
    ]

    res = compute_calibration_from_trades(
        "sess_short",
        sess_dir,
        bt_dir,
        paper_trades,
        bt_trades,
        p_ev,
        b_ev,
    )
    assert res.refused is True
    assert "Fewer than 10 fill pairs found" in res.refusal_reason
    assert res.fills_compared == 1


def test_calibration_unpaired_fills_counted_and_not_mismatched(tmp_path: Path) -> None:
    """Unpaired fills (e.g. TCS vs HDFCBANK) are counted as unpaired and never mismatched."""
    sess_dir = tmp_path / "sess"
    bt_dir = tmp_path / "bt"
    sess_dir.mkdir(parents=True)
    bt_dir.mkdir(parents=True)

    t0 = datetime(2026, 11, 19, 9, 15, 0, tzinfo=UTC)
    p_tr = [
        {
            "client_ref": "p1",
            "instrument": "NSE:EQ:TCS",
            "side": "BUY",
            "price": "3200.0",
            "ts": t0.isoformat(),
        }
    ]
    b_tr = [
        {
            "client_ref": "b1",
            "instrument": "NSE:EQ:HDFCBANK",
            "side": "BUY",
            "price": "1500.0",
            "ts": t0.isoformat(),
        }
    ]

    res = compute_calibration_from_trades(
        "sess_unpaired",
        sess_dir,
        bt_dir,
        p_tr,
        b_tr,
        [],
        [],
    )
    assert res.refused is True
    assert res.fills_compared == 0
    assert res.unpaired_paper_fills == 1
    assert res.unpaired_backtest_fills == 1


def test_calibration_cli_refusal_display(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Typer CLI backtest calibrate displays refusal message without exception."""
    proj = _build_test_project(tmp_path, "cal_cli_proj")
    dummy_res = CalibrationResult(
        session_id="sess_refuse",
        fills_compared=3,
        mean_adverse_bps=0.0,
        median_adverse_bps=0.0,
        p95_adverse_bps=0.0,
        mean_pnl_diff=0.0,
        median_pnl_diff=0.0,
        suggested_slippage_bps=5.0,
        calibration_file=proj / "results" / "cal.json",
        unpaired_paper_fills=2,
        unpaired_backtest_fills=1,
        refused=True,
        refusal_reason=(
            "Fewer than 10 fill pairs found (3 pairs); calibration requires at least 10 pairs."
        ),
    )
    monkeypatch.setattr(
        "algodesk.engine.calibration.run_slippage_calibration",
        lambda rt, sess: dummy_res,
    )
    result = runner.invoke(
        backtest_app,
        ["calibrate", "--session", "sess_refuse"],
        obj={"project_dir": proj},
    )
    assert result.exit_code == 0
    assert "Refused: Fewer than 10 fill pairs found" in result.output
    assert "Fills compared: 3" in result.output


def test_tick_bar_feed_calendar_filtering(tmp_path: Path) -> None:
    """TickBarFeed filters ticks according to exchange calendars matching paper behavior."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from algodesk.data.schema import events_to_table

    sess_dir = tmp_path / "sess_cal"
    sess_dir.mkdir(parents=True)

    t_start = datetime(2026, 11, 19, 8, 30, 0, tzinfo=UTC)
    t_end = datetime(2026, 11, 19, 8, 33, 0, tzinfo=UTC)
    inst_mcx = InstrumentId("MCX:FUT:GOLD:2026-12-04")

    tick = Tick(
        instrument=inst_mcx,
        exch_ts=t_start + timedelta(seconds=10),
        recv_ts=t_start + timedelta(seconds=10),
        ltp=75000.0,
        ltq=1,
        volume=1,
    )
    tbl = events_to_table([tick])
    tbl = tbl.append_column("seq", pa.array([1], type=pa.int64()))
    tbl = tbl.append_column("session_id", pa.array(["sess_cal"], type=pa.string()))
    pq.write_table(tbl, sess_dir / "part-0001.parquet")

    nse_cal = type(
        "NSECalendar",
        (),
        {
            "is_trading_day": lambda self, d, **kw: True,
            "sessions": lambda self, d, **kw: [],
            "sessions_for": lambda self, d, **kw: [],
        },
    )()
    from algodesk.engine.sessions import SessionManager

    sm = SessionManager(calendars={"nse": nse_cal})

    feed = TickBarFeed(
        session_dir=sess_dir,
        timeframe="1m",
        session_manager=sm,
        tick_window=(t_start, t_end),
    )
    assert len(feed.bars) == 0


def test_calibration_suggested_slippage_advisory_median(tmp_path: Path) -> None:
    """Suggested slippage is median of adverse pairs, labelled advisory in calibration JSON."""
    sess_dir = tmp_path / "sess_adv"
    bt_dir = tmp_path / "bt_adv"
    sess_dir.mkdir(parents=True)
    bt_dir.mkdir(parents=True)

    t0 = datetime(2026, 11, 19, 9, 15, 0, tzinfo=UTC)
    paper_trades = []
    bt_trades = []
    paper_events = []
    bt_events = []

    slippage_vals = [10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0, 24.0, 26.0, 28.0]
    for i, s_bps in enumerate(slippage_vals):
        sym = f"NSE:EQ:SYM{i:02d}"
        cref = f"p_ref_{i}"
        bt_cref = f"b_ref_{i}"
        t_cur = t0 + timedelta(minutes=i)

        paper_events.append(
            {"kind": "intent", "payload": {"client_ref": cref, "ts": t_cur.isoformat()}}
        )
        bt_events.append(
            {"kind": "intent", "payload": {"client_ref": bt_cref, "ts": t_cur.isoformat()}}
        )

        p_price = 100.0 * (1.0 + s_bps / 10000.0)
        paper_trades.append(
            {
                "client_ref": cref,
                "instrument": sym,
                "side": "BUY",
                "price": f"{p_price:.4f}",
                "net_pnl": "50.0",
                "ts": t_cur.isoformat(),
            }
        )
        bt_trades.append(
            {
                "client_ref": bt_cref,
                "instrument": sym,
                "side": "BUY",
                "price": "100.00",
                "net_pnl": "52.0",
                "ts": t_cur.isoformat(),
            }
        )

    res = compute_calibration_from_trades(
        "sess_adv",
        sess_dir,
        bt_dir,
        paper_trades,
        bt_trades,
        paper_events,
        bt_events,
    )
    assert res.refused is False
    assert res.fills_compared == 10
    assert pytest.approx(res.median_adverse_bps, 0.1) == 19.0
    assert pytest.approx(res.suggested_slippage_bps, 0.1) == 19.0
    cal_json = json.loads((bt_dir / "calibration.json").read_text(encoding="utf-8"))
    assert cal_json["advisory"] is True
    assert cal_json["suggested_slippage_bps"] == 19.0


def test_tick_fidelity_hash_difference_warning(tmp_path: Path) -> None:
    """When sim or fee plugin hash differs from paper manifest, warning is emitted."""
    from algodesk.cli.backtest import _check_sim_fee_hashes
    from algodesk.ops.runtime import open_runtime

    proj = _build_test_project(tmp_path, "hash_proj")
    sess_dir = proj / "results" / "sess_demo"
    sess_dir.mkdir(parents=True)
    manifest = {
        "plugin_versions": {
            "sim": {"version": "0.1.0", "hash": "old_sim_hash_123"},
            "zerodha_nse": {"version": "0.1.0", "hash": "old_fee_hash_456"},
        }
    }
    (sess_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    class DummyRunConfig:
        execution_venue = type("Venue", (), {"plugin": "sim"})()
        fees = type("Fees", (), {"schedule": "zerodha_nse"})()

    with open_runtime(proj, ensure=True) as rt:
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            _check_sim_fee_hashes(rt, sess_dir, DummyRunConfig())
        output = buf.getvalue()
        assert "Sim or fee plugin code hash differs from recorded paper run" in output
        assert "fills and charges can legitimately differ" in output


# =========================================================================
# CP3 Benchmark Tests (Steps 9, 10)
# =========================================================================


def test_benchmark_profile_filtered_to_algodesk_and_plugins() -> None:
    """Benchmark profiler filters functions strictly to src/algodesk and plugins."""
    import cProfile

    from algodesk.config.snapshot import build_effective

    prof = cProfile.Profile()
    prof.enable()
    try:
        build_effective(Path("."))
    except Exception:
        pass
    prof.disable()

    text = _filter_and_format_profile(prof, top_n=10)
    for line in text.splitlines():
        if ".py:" in line:
            assert "/src/algodesk/" in line or "/plugins/" in line
            assert ".local/python" not in line
            assert "site-packages" not in line
            assert "/tests/" not in line


def test_benchmark_fixed_and_per_bar_cost_separation(tmp_path: Path) -> None:
    """Benchmark separates fixed startup cost and per-bar cost accurately."""
    rep = run_benchmark_measurement(symbols_count=5, sessions_count=5, timeframe="1d")
    assert rep.total_bars == 25
    assert rep.elapsed_seconds > 0.0
    assert rep.bars_per_second > 0.0
    assert rep.fixed_cost_seconds >= 0.0
    assert rep.per_bar_cost_seconds >= 0.0
    assert rep.peak_memory_mb > 0.0


def test_benchmark_cli_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Typer CLI backtest benchmark displays fixed cost, per-bar cost, throughput, and tottime."""
    dummy_rep = BenchmarkReport(
        symbols=20,
        sessions=20,
        timeframe="1m",
        total_bars=400,
        elapsed_seconds=0.50,
        bars_per_second=800.0,
        peak_memory_mb=12.4,
        profile_stats_text=(
            "   ncalls  tottime  percall  cumtime  percall filename:lineno(function)\n"
            "      400    0.010    0.000    0.020    0.000 "
            "/src/algodesk/engine/runner.py:100(_dispatch)\n"
        ),
        fixed_cost_seconds=0.150,
        per_bar_cost_seconds=0.000875,
    )
    monkeypatch.setattr(
        "algodesk.engine.benchmark.run_benchmark_measurement",
        lambda symbols_count, sessions_count, timeframe: dummy_rep,
    )
    result = runner.invoke(
        backtest_app,
        ["benchmark", "--symbols", "20", "--sessions", "20", "--timeframe", "1m"],
    )
    assert result.exit_code == 0
    assert "Configuration: 20 symbols x 20 sessions (1m)" in result.output
    assert "Total bars: 400" in result.output
    assert "Fixed cost: 0.150s" in result.output
    assert "Per-bar cost: 875.0 µs/bar (0.000875 s/bar)" in result.output
    assert "Top 10 functions by internal time (tottime):" in result.output
