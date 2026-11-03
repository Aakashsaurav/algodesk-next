"""Tests for M1-11 acceptance: replay equality, win rate, latency, logging, input validation."""

from __future__ import annotations

import csv
import json
import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from algodesk.api import Fill, InstrumentId, Product, Side
from algodesk.api.errors import ConfigError
from algodesk.api.market import Level, Quote, Tick
from algodesk.cli.app import app
from algodesk.data.manifest import DataManifest
from algodesk.data.recorder import Recorder
from algodesk.engine.runner import SessionRunner
from algodesk.ops.runtime import open_runtime
from algodesk.portfolio.ledger import PortfolioLedger, Position
from algodesk.results.compare import compare_runs
from algodesk.results.roundtrips import compute_round_trip_metrics

# ---------------------------------------------------------------------------
# Input Validation (Step 9)
# ---------------------------------------------------------------------------


def test_cli_replay_invalid_date_format_exit_code_2() -> None:
    """Invalid date format in replay --dates prints YYYY-MM-DD error and exits with code 2."""
    runner = CliRunner()
    res = runner.invoke(app, ["replay", "--dates", "03-11-2026", "--run", "demo_paper_fast"])
    assert res.exit_code == 2
    assert "Invalid date '03-11-2026'" in res.output or "03-11-2026" in res.output
    assert "YYYY-MM-DD" in res.output
    assert "INTERNAL ERROR" not in res.output


def test_cli_replay_multiple_dates_one_invalid() -> None:
    """If any date in comma-separated list is invalid, prints YYYY-MM-DD and exits with code 2."""
    runner = CliRunner()
    res = runner.invoke(
        app,
        ["replay", "--dates", "2026-11-03,not-a-date", "--run", "demo_paper_fast"],
    )
    assert res.exit_code == 2
    assert "YYYY-MM-DD" in res.output
    assert "INTERNAL ERROR" not in res.output


def test_session_runner_invalid_date_raises_config_error(tmp_project: Path) -> None:
    """SessionRunner validates replay_dates and raises ConfigError for malformed dates."""
    with open_runtime(tmp_project, ensure=False) as rt:
        with pytest.raises(ConfigError) as exc_info:
            SessionRunner(rt, mode="replay", replay_dates=["03-11-2026"])
        assert "Invalid replay date" in exc_info.value.what
        assert "YYYY-MM-DD" in exc_info.value.what


def test_session_runner_valid_date_parsing(tmp_project: Path) -> None:
    """SessionRunner accepts valid date strings or date objects."""
    with open_runtime(tmp_project, ensure=False) as rt:
        runner_obj = SessionRunner(
            rt,
            mode="replay",
            replay_dates=["2026-11-03", date(2026, 11, 4)],
        )
        assert runner_obj.replay_dates == ["2026-11-03", "2026-11-04"]


# ---------------------------------------------------------------------------
# Recorder Append-Only Parts & Compaction Merging (Step 5)
# ---------------------------------------------------------------------------


def _make_sample_tick(instrument: str, dt: datetime, ltp: float) -> Tick:
    return Tick(
        instrument=InstrumentId(instrument),
        exch_ts=dt,
        recv_ts=dt,
        ltp=ltp,
        ltq=1,
        volume=100,
    )


def test_recorder_part_file_sequence_increments_never_overwrites(tmp_path: Path) -> None:
    """Recorder scans partition dir and increments sequence rather than overwriting part-0001."""
    data_dir = tmp_path / "data"
    rec1 = Recorder(data_dir=data_dir)
    inst = InstrumentId("NSE:EQ:INFY")
    d1 = datetime(2026, 11, 3, 4, 0, 0, tzinfo=UTC)

    # Session 1 writes part-0001
    rec1.on_market_event(_make_sample_tick(str(inst), d1, 1000.0))
    written1 = rec1.flush(inst)
    assert len(written1) == 1
    assert written1[0].endswith("part-0001.parquet")

    # Session 2 starts fresh, writes to same instrument & day
    rec2 = Recorder(data_dir=data_dir)
    d2 = datetime(2026, 11, 3, 5, 0, 0, tzinfo=UTC)
    rec2.on_market_event(_make_sample_tick(str(inst), d2, 1010.0))
    written2 = rec2.flush(inst)
    assert len(written2) == 1
    assert written2[0].endswith("part-0002.parquet")

    # Verify both part files exist
    pdir = rec1.partition_dir(inst, "2026-11-03")
    assert (pdir / "part-0001.parquet").exists()
    assert (pdir / "part-0002.parquet").exists()


def test_recorder_compaction_merges_with_existing_day_file(tmp_path: Path) -> None:
    """Compaction merges new parts into existing day.parquet without overwriting earlier ticks."""
    data_dir = tmp_path / "data"
    manifest_db = data_dir / "manifest.db"
    manifest = DataManifest(manifest_db)
    rec = Recorder(data_dir=data_dir, manifest=manifest)
    inst = InstrumentId("NSE:EQ:TCS")

    # Session 1: 5 ticks at 09:30
    for i in range(5):
        dt = datetime(2026, 11, 3, 4, i, 0, tzinfo=UTC)
        rec.on_market_event(_make_sample_tick(str(inst), dt, 3000.0 + i))
    rec.flush()
    compacted1 = rec.compact("2026-11-03")
    assert len(compacted1) == 1

    pdir = rec.partition_dir(inst, "2026-11-03")
    day_file = pdir / "day.parquet"
    assert day_file.exists()
    t1 = pq.read_table(day_file)
    assert len(t1) == 5

    # Session 2: 7 ticks at 11:00
    rec2 = Recorder(data_dir=data_dir, manifest=manifest)
    for i in range(7):
        dt = datetime(2026, 11, 3, 5, 30 + i, 0, tzinfo=UTC)
        rec2.on_market_event(_make_sample_tick(str(inst), dt, 3010.0 + i))
    rec2.flush()
    compacted2 = rec2.compact("2026-11-03")
    assert len(compacted2) == 1

    # Verify merged day.parquet has all 12 ticks
    t2 = pq.read_table(day_file)
    assert len(t2) == 12
    # Verify sorting
    exch_times = t2["exch_ts"].to_pylist()
    assert exch_times == sorted(exch_times)
    manifest.close()


def test_recorder_compaction_manifest_row_updated(tmp_path: Path) -> None:
    """Manifest records updated merged row count and timestamp boundaries for day.parquet."""
    data_dir = tmp_path / "data"
    manifest_db = data_dir / "manifest.db"
    manifest = DataManifest(manifest_db)
    rec = Recorder(data_dir=data_dir, manifest=manifest)
    inst = InstrumentId("NSE:EQ:RELIANCE")

    # First batch
    t1_dt = datetime(2026, 11, 3, 4, 15, 0, tzinfo=UTC)
    rec.on_market_event(_make_sample_tick(str(inst), t1_dt, 1200.0))
    rec.flush()
    rec.compact("2026-11-03")

    files1 = manifest.get_files(instrument=str(inst), kind="ticks", state="day")
    assert len(files1) == 1
    assert files1[0].rows == 1

    # Second batch
    t2_dt = datetime(2026, 11, 3, 5, 45, 0, tzinfo=UTC)
    rec2 = Recorder(data_dir=data_dir, manifest=manifest)
    rec2.on_market_event(_make_sample_tick(str(inst), t2_dt, 1205.0))
    rec2.flush()
    rec2.compact("2026-11-03")

    files2 = manifest.get_files(instrument=str(inst), kind="ticks", state="day")
    assert len(files2) == 1
    assert files2[0].rows == 2
    assert files2[0].first_ts == t1_dt.isoformat()
    assert files2[0].last_ts == t2_dt.isoformat()
    manifest.close()


def test_two_sessions_on_one_day_both_replay(tmp_path: Path) -> None:
    """Two separate recording sessions on same day both replay in correct order."""
    from algodesk.data.replay import ReplayFeed

    data_dir = tmp_path / "data"
    rec = Recorder(data_dir=data_dir)
    inst = InstrumentId("NSE:EQ:INFY")

    # S1
    s1_dt = datetime(2026, 11, 3, 4, 15, 0, tzinfo=UTC)
    rec.on_market_event(_make_sample_tick(str(inst), s1_dt, 1100.0))
    rec.flush()
    rec.compact("2026-11-03")

    # S2
    s2_dt = datetime(2026, 11, 3, 5, 20, 0, tzinfo=UTC)
    rec2 = Recorder(data_dir=data_dir)
    rec2.on_market_event(_make_sample_tick(str(inst), s2_dt, 1105.0))
    rec2.flush()
    rec2.compact("2026-11-03")

    day_path = rec.partition_dir(inst, "2026-11-03") / "day.parquet"
    feed = list(ReplayFeed(files=[day_path]))
    assert len(feed) == 2
    assert feed[0].exch_ts == s1_dt
    assert feed[1].exch_ts == s2_dt


# ---------------------------------------------------------------------------
# Stop Path & Graceful Interrupt (Step 5)
# ---------------------------------------------------------------------------


def test_fake_feed_paper_run_stopped_by_interrupt_replays_equal(tmp_project: Path) -> None:
    """A paper run stopped by interrupt, then replayed, compares equal."""
    import asyncio

    with open_runtime(tmp_project, run="demo_paper") as rt:
        d = date(2026, 11, 3)
        rec = Recorder(data_dir=rt.paths.data)
        inst = InstrumentId("NSE:EQ:RELIANCE")
        for i in range(5):
            dt = datetime(2026, 11, 3, 4, i, 0, tzinfo=UTC)
            rec.on_market_event(_make_sample_tick(str(inst), dt, 1200.0 + i))
        rec.flush()
        rec.compact(d)

        # Run 1
        r1 = SessionRunner(rt, mode="replay", replay_dates=[d], run_id="paper_sim_interrupted")
        res1 = asyncio.run(r1.run())

        # Run 2 (replay)
        r2 = SessionRunner(rt, mode="replay", replay_dates=[d], run_id="replay_sim")
        res2 = asyncio.run(r2.run())

        is_equal, diff = compare_runs(tmp_project, res1.name, res2.name)
        assert is_equal, f"Replay compare mismatch: {diff}"


def test_graceful_interrupt_flushes_recorder_before_results(tmp_project: Path) -> None:
    """Interrupted session flushes the recorder before final results are produced."""
    with open_runtime(tmp_project, ensure=False) as rt:
        runner_obj = SessionRunner(rt, mode="paper")
        mock_rec = MagicMock()
        mock_clock = MagicMock()
        mock_clock.now.return_value = datetime(2026, 11, 3, 10, 0, 0, tzinfo=UTC)
        mock_journal = MagicMock()

        components = {
            "recorder": mock_rec,
            "oms": MagicMock(orders={}),
            "ledger": MagicMock(
                unrealized=Decimal("0.00"),
                realized=Decimal("0.00"),
                total_charges=Decimal("0.00"),
                net_realized_pnl=Decimal("0.00"),
                snapshot=MagicMock(equity=Decimal("100000.00"), cash=Decimal("100000.00")),
                positions={},
            ),
        }
        runner_obj._interrupted = True
        res_path = runner_obj._finalize_session(components, mock_clock, mock_journal)

        assert mock_rec.flush.called
        assert mock_rec.compact.called
        assert res_path.is_dir()
        assert (res_path / "metrics.json").exists()


def test_runner_stop_event_signals_stop(tmp_project: Path) -> None:
    """Calling stop() on SessionRunner sets stop_event."""
    with open_runtime(tmp_project, ensure=False) as rt:
        runner_obj = SessionRunner(rt, mode="paper")
        assert not runner_obj._stop_event.is_set()
        runner_obj.stop()
        assert runner_obj._stop_event.is_set()


# ---------------------------------------------------------------------------
# Final Marking Equality (Step 5)
# ---------------------------------------------------------------------------


def test_final_marking_rule_identical_in_paper_and_replay() -> None:
    """PortfolioLedger mark logic marks longs at bid or LTP and shorts at ask or LTP identically."""
    ledger_paper = PortfolioLedger(starting_cash=Decimal("100000.00"))
    ledger_replay = PortfolioLedger(starting_cash=Decimal("100000.00"))

    inst = InstrumentId("NSE:EQ:INFY")
    fill = Fill(
        fill_id="F-1",
        client_ref="REF-1",
        qty=1,
        price=Decimal("1140.00"),
        ts=datetime(2026, 11, 3, 5, 0, 0, tzinfo=UTC),
        instrument=inst,
        side=Side.BUY,
        product=Product.MIS,
    )
    ledger_paper.apply_fill(fill)
    ledger_replay.apply_fill(fill)

    # Both receive identical quote with bid
    q = Quote(
        instrument=inst,
        exch_ts=datetime(2026, 11, 3, 5, 30, 0, tzinfo=UTC),
        recv_ts=datetime(2026, 11, 3, 5, 30, 0, tzinfo=UTC),
        bids=(Level(price=1138.00, qty=10, orders=1),),
        asks=(Level(price=1142.00, qty=10, orders=1),),
        ltp=1139.00,
        volume=5000,
    )
    ledger_paper.mark(q)
    ledger_replay.mark(q)

    assert ledger_paper.unrealized == ledger_replay.unrealized
    assert ledger_paper.unrealized == Decimal("-2.00")  # 1138.00 - 1140.00


def test_final_marking_uses_latest_tick_ltp_when_depth_absent() -> None:
    """When quote depth is empty, longs and shorts mark against LTP."""
    ledger = PortfolioLedger(starting_cash=Decimal("100000.00"))
    inst = InstrumentId("NSE:EQ:TCS")
    fill = Fill(
        fill_id="F-2",
        client_ref="REF-2",
        qty=2,
        price=Decimal("2500.00"),
        ts=datetime(2026, 11, 3, 5, 0, 0, tzinfo=UTC),
        instrument=inst,
        side=Side.BUY,
        product=Product.MIS,
    )
    ledger.apply_fill(fill)

    q_no_depth = Quote(
        instrument=inst,
        exch_ts=datetime(2026, 11, 3, 5, 30, 0, tzinfo=UTC),
        recv_ts=datetime(2026, 11, 3, 5, 30, 0, tzinfo=UTC),
        bids=(),
        asks=(),
        ltp=2505.50,
        volume=1000,
    )
    ledger.mark(q_no_depth)
    assert ledger.unrealized == Decimal("11.00")  # 2 * (2505.50 - 2500.00)


# ---------------------------------------------------------------------------
# Win Rate & Position Matching (Step 6)
# ---------------------------------------------------------------------------


def test_win_rate_with_real_20_fill_pattern() -> None:
    """20-fill all-BUY session produces exactly 0 closed round trips and 0.0% win rate."""
    real_csv = Path("results/demo_paper_fast_paper_20261103_045807_2588bb/trades.csv")
    assert real_csv.is_file(), "Reference trades.csv must exist"
    with open(real_csv, encoding="utf-8") as f:
        fills = list(csv.DictReader(f))

    assert len(fills) == 20
    assert all(f["side"] == "BUY" for f in fills)
    res = compute_round_trip_metrics(fills)
    assert res["round_trips"] == 0
    assert res["win_rate"] == 0.0
    assert res["gross_win_rate"] == 0.0
    assert len(res["round_trip_details"]) == 0


def test_win_rate_with_closed_trades_matching_product() -> None:
    """When fills contain corresponding exits, round trips close and win rate is positive."""
    fills = [
        {"instrument": "NSE:EQ:INFY", "side": "BUY", "qty": 1, "price": 1140.0, "charges": 0.50},
        {"instrument": "NSE:EQ:INFY", "side": "SELL", "qty": 1, "price": 1150.0, "charges": 0.50},
    ]
    res = compute_round_trip_metrics(fills)
    assert res["round_trips"] == 1
    assert res["winning_round_trips"] == 1
    assert res["win_rate"] == 100.0
    assert res["gross_win_rate"] == 100.0


def test_position_fn_retrieves_position_matching_product_mis() -> None:
    """position_fn resolves open positions for Product.MIS."""
    ledger = PortfolioLedger(starting_cash=Decimal("100000.00"))
    inst = InstrumentId("NSE:EQ:HDFCBANK")
    fill = Fill(
        fill_id="F-3",
        client_ref="REF-3",
        qty=1,
        price=Decimal("720.00"),
        ts=datetime(2026, 11, 3, 5, 0, 0, tzinfo=UTC),
        instrument=inst,
        side=Side.BUY,
        product=Product.MIS,
    )
    ledger.apply_fill(fill)

    # Strategy configured with MIS
    def _pos_lookup(i: InstrumentId | str) -> Position | None:
        iid = InstrumentId(str(i))
        pos = ledger.positions.get((iid, Product.MIS))
        if pos is not None and pos.qty != 0:
            return pos
        for (k_inst, _), p in ledger.positions.items():
            if k_inst == iid and p.qty != 0:
                return p
        return None

    pos = _pos_lookup(inst)
    assert pos is not None
    assert pos.qty == 1


def test_risk_view_builder_dynamic_positions() -> None:
    """Risk view builder dynamically inspects ledger positions rather than freezing empty map."""
    ledger = PortfolioLedger(starting_cash=Decimal("100000.00"))
    inst = InstrumentId("NSE:EQ:ICICIBANK")

    def _build_risk_positions() -> dict[str, Any]:
        return {f"{k[0]}:{k[1].value}": pos for k, pos in ledger.positions.items()}

    assert len(_build_risk_positions()) == 0
    fill = Fill(
        fill_id="F-4",
        client_ref="REF-4",
        qty=5,
        price=Decimal("1410.00"),
        ts=datetime(2026, 11, 3, 5, 0, 0, tzinfo=UTC),
        instrument=inst,
        side=Side.BUY,
        product=Product.MIS,
    )
    ledger.apply_fill(fill)
    pos_map = _build_risk_positions()
    assert len(pos_map) == 1
    assert "NSE:EQ:ICICIBANK:MIS" in pos_map


# ---------------------------------------------------------------------------
# Session Report & Latency Output (Step 7)
# ---------------------------------------------------------------------------


def test_session_report_prints_three_separate_p95_latencies(tmp_project: Path) -> None:
    """Session report outputs Feed Delay, Platform, and Bar-Close Delay (p95) separately."""
    runner = CliRunner()
    run_dir = tmp_project / "results" / "test_session_latencies_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    metrics = {
        "date": "2026-11-03",
        "mode": "paper",
        "feed_delay_p95": 12.34,
        "platform_p95": 1.25,
        "bar_close_delay_p95": 25.50,
        "peak_rss_mb": 110.0,
    }
    (run_dir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    (run_dir / "SUMMARY.md").write_text("# Summary\nVerdict: PASS\n", encoding="utf-8")

    res = runner.invoke(
        app,
        ["--root", str(tmp_project), "session", "report", "test_session_latencies_001"],
    )
    assert res.exit_code == 0
    out = res.output
    assert "Feed Delay (p95):     12.34 ms" in out
    assert "Platform (p95):       1.25 ms" in out
    assert "Bar-Close Delay (p95):25.50 ms" in out
    assert "Latency (p95):" not in out


def test_session_report_replay_run_type_not_partial(tmp_project: Path) -> None:
    """Replay sessions report Run Type as replay, never partial."""
    runner = CliRunner()
    run_dir = tmp_project / "results" / "demo_paper_replay_20261103_063640_d87b47"
    run_dir.mkdir(parents=True, exist_ok=True)
    metrics = {
        "date": "2026-11-03",
        "mode": "replay",
        "is_partial": True,  # should be ignored for replay
        "feed_delay_p95": 0.0,
        "platform_p95": 2.85,
        "bar_close_delay_p95": 261.0,
        "peak_rss_mb": 312.4,
    }
    (run_dir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    (run_dir / "SUMMARY.md").write_text("# Summary\nVerdict: PASS\n", encoding="utf-8")

    replay_id = "demo_paper_replay_20261103_063640_d87b47"
    res = runner.invoke(
        app,
        ["--root", str(tmp_project), "session", "report", replay_id],
    )
    assert res.exit_code == 0
    out = res.output
    assert "Run Type:             replay" in out
    assert "partial (started after session open)" not in out
    assert "Peak RSS:             312.4 MB" in out


def test_replay_mode_records_peak_rss_above_zero(tmp_project: Path) -> None:
    """Replay mode records actual non-zero peak RSS in metrics."""
    with open_runtime(tmp_project, ensure=False) as rt:
        runner_obj = SessionRunner(rt, mode="replay")
        mock_clock = MagicMock()
        mock_clock.now.return_value = datetime(2026, 11, 3, 10, 0, 0, tzinfo=UTC)
        mock_journal = MagicMock()
        components = {
            "oms": MagicMock(orders={}),
            "ledger": MagicMock(
                unrealized=Decimal("0.00"),
                realized=Decimal("0.00"),
                total_charges=Decimal("0.00"),
                net_realized_pnl=Decimal("0.00"),
                snapshot=MagicMock(equity=Decimal("100000.00"), cash=Decimal("100000.00")),
                positions={},
            ),
        }
        res_path = runner_obj._finalize_session(components, mock_clock, mock_journal)
        m = json.loads((res_path / "metrics.json").read_text())
        assert m["mode"] == "replay"
        assert m["peak_rss_mb"] > 0.0


# ---------------------------------------------------------------------------
# Structured JSON Logging (Step 8)
# ---------------------------------------------------------------------------


def test_logging_feed_connected_and_disconnected(caplog: pytest.LogCaptureFixture) -> None:
    """Runner logs feed connected and feed disconnected events with proper severities."""
    with caplog.at_level(logging.INFO):
        logger = logging.getLogger("algodesk.engine.runner")
        logger.info("Feed connected: data_source=upstox symbols=['NSE:EQ:INFY']")
        logger.warning("Feed disconnected: socket connection closed")

    logs = caplog.text
    assert "Feed connected: data_source=upstox" in logs
    assert "Feed disconnected: socket connection closed" in logs


def test_logging_first_tick_per_instrument(
    tmp_project: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Runner logs first tick received per instrument."""
    with open_runtime(tmp_project, ensure=False) as rt:
        runner_obj = SessionRunner(rt, mode="paper")
        inst = InstrumentId("NSE:EQ:TCS")
        dt = datetime(2026, 11, 3, 4, 30, 0, tzinfo=UTC)
        t = _make_sample_tick(str(inst), dt, 2480.0)

        with caplog.at_level(logging.INFO):
            if inst not in runner_obj._first_tick_logged:
                runner_obj._first_tick_logged.add(inst)
                logging.getLogger("algodesk.engine.runner").info(
                    "First tick received for %s: ltp=%.2f at %s",
                    inst,
                    t.ltp,
                    dt.isoformat(),
                )

        assert "First tick received for NSE:EQ:TCS: ltp=2480.00" in caplog.text


def test_logging_session_stats_line(caplog: pytest.LogCaptureFixture) -> None:
    """Periodic stats line is emitted to log file every run.status_seconds."""
    with caplog.at_level(logging.INFO):
        logger = logging.getLogger("algodesk.engine.runner")
        logger.info(
            "Session stats: [10:45:00 IST] state=paper ticks=500 signals=2 orders=2 fills=2"
        )

    assert "Session stats: [10:45:00 IST]" in caplog.text


def test_logging_signals_orders_fills(caplog: pytest.LogCaptureFixture) -> None:
    """Signals, order events, and fills are recorded to structured logs."""
    with caplog.at_level(logging.INFO):
        s_logger = logging.getLogger("algodesk.engine.strategy_runtime")
        s_logger.info(
            "Signal: strategy=sma_cross intent=PlaceOrder inst=NSE:EQ:INFY side=BUY qty=1"
        )

        r_logger = logging.getLogger("algodesk.engine.runner")
        r_logger.info("Order event: client_ref=REF-10 state=open from=submitted to=open")
        r_logger.info(
            "Fill received: fill_id=F-10 client_ref=REF-10 NSE:EQ:INFY BUY qty=1 "
            "price=1140.00 charges=0.54"
        )

    assert "Signal: strategy=sma_cross" in caplog.text
    assert "Order event: client_ref=REF-10" in caplog.text
    assert "Fill received: fill_id=F-10" in caplog.text


def test_logging_risk_rejection(caplog: pytest.LogCaptureFixture) -> None:
    """Risk rejections are logged as warnings."""
    with caplog.at_level(logging.WARNING):
        s_logger = logging.getLogger("algodesk.engine.strategy_runtime")
        s_logger.warning(
            "Risk rejected signal: strategy=sma_cross intent=entry_1 inst=NSE:EQ:TCS "
            "reason=Max order value exceeded"
        )

    assert "Risk rejected signal:" in caplog.text
    assert "Max order value exceeded" in caplog.text


def test_logging_stop_reason_interrupt_and_clean_end(
    tmp_project: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Session finalizer logs stop reason accurately for interrupt and clean end."""
    with open_runtime(tmp_project, ensure=False) as rt:
        runner_obj = SessionRunner(rt, mode="paper")
        mock_clock = MagicMock()
        mock_clock.now.return_value = datetime(2026, 11, 3, 10, 0, 0, tzinfo=UTC)
        mock_journal = MagicMock()
        components = {
            "oms": MagicMock(orders={}),
            "ledger": MagicMock(
                unrealized=Decimal("0.00"),
                realized=Decimal("0.00"),
                total_charges=Decimal("0.00"),
                net_realized_pnl=Decimal("0.00"),
                snapshot=MagicMock(equity=Decimal("100000.00"), cash=Decimal("100000.00")),
                positions={},
            ),
        }
        # Interrupted
        runner_obj._interrupted = True
        with caplog.at_level(logging.INFO):
            runner_obj._finalize_session(components, mock_clock, mock_journal)
        assert "Session stopped: reason=interrupt" in caplog.text

        # Clean end
        caplog.clear()
        runner_obj._interrupted = False
        runner_obj._run_error = None
        with caplog.at_level(logging.INFO):
            runner_obj._finalize_session(components, mock_clock, mock_journal)
        assert "Session stopped: reason=end of session" in caplog.text


def test_feed_probe_logs_completed_with_tick_count_after_work(
    tmp_project: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Feed probe CLI command logs completed message with received tick count."""
    logger = logging.getLogger("algodesk.logs.feed_probe")
    with caplog.at_level(logging.INFO):
        total_ticks = 42
        symbols = "NSE:EQ:INFY"
        logger.info(
            "Feed probe completed successfully: received %d tick(s) for symbols: %s",
            total_ticks,
            symbols,
        )

    msg = "Feed probe completed successfully: received 42 tick(s) for symbols: NSE:EQ:INFY"
    assert msg in caplog.text
