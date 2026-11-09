"""Summary: Unit, integration, and property tests for M1-13 deterministic replay.

Covers monotonic tick sequence numbers stored in Parquet recordings, ReplayFeed sequence ordering,
BarBuilder late tick rejection and deterministic timer bar closes, graceful interrupt handling
without destroyed task warnings, STOPPED verdicts, and results compare signal/fill reporting.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from algodesk.api.market import Bar, InstrumentId, Tick
from algodesk.cli import app
from algodesk.data.manifest import DataManifest
from algodesk.data.recorder import Recorder
from algodesk.data.replay import ReplayFeed
from algodesk.engine.bars import BarBuilder
from algodesk.engine.bus import EventBus
from algodesk.engine.events import SystemEvent
from algodesk.engine.runner import SessionRunner
from algodesk.ops.runtime import open_runtime
from algodesk.results.compare import compare_runs

IST = ZoneInfo("Asia/Kolkata")


def _make_tick(
    inst: str,
    exch_ts: datetime,
    ltp: float,
    volume: int,
    recv_ts: datetime | None = None,
) -> Tick:
    return Tick(
        instrument=InstrumentId(inst),
        exch_ts=exch_ts,
        recv_ts=recv_ts or (exch_ts + timedelta(milliseconds=50)),
        ltp=ltp,
        ltq=1,
        volume=volume,
    )


class _SingleListStream:
    """Synchronous factory returning an async iterator over items."""

    def __init__(self, items: list[tuple[Any, int | None]]) -> None:
        self._items = items
        self._idx = 0

    def __aiter__(self) -> _SingleListStream:
        return self

    async def __anext__(self) -> tuple[Any, int | None]:
        if self._idx < len(self._items):
            item = self._items[self._idx]
            self._idx += 1
            return item
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self._idx = len(self._items)


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


def test_tick_sequence_number_assignment_and_recording_in_parquet(tmp_path: Path) -> None:
    """Monotonic sequence numbers are assigned and stored in recorded Parquet partitions."""
    data_dir = tmp_path / "data"
    manifest_db = tmp_path / "manifest.db"
    manifest = DataManifest(manifest_db)

    recorder = Recorder(
        data_dir=data_dir,
        manifest=manifest,
        max_buffer_rows=2,
    )

    t0 = datetime(2026, 11, 6, 9, 15, 0, tzinfo=UTC)
    t1 = _make_tick("NSE:EQ:INFY", t0, 100.0, 10)
    t2 = _make_tick("NSE:EQ:INFY", t0 + timedelta(seconds=1), 101.0, 20)

    recorder.on_market_event(t1, seq=101)
    recorder.on_market_event(t2, seq=102)
    recorder.close()

    files = manifest.get_files(instrument="NSE:EQ:INFY")
    assert len(files) >= 1
    table = pq.read_table(files[0].path)
    assert "seq" in table.column_names
    seqs = table.column("seq").to_pylist()
    assert seqs == [101, 102]


def test_replay_feed_orders_by_sequence_numbers(tmp_path: Path) -> None:
    """ReplayFeed merges files using monotonic sequence numbers, not delayed exchange time."""
    data_dir = tmp_path / "data"
    manifest_db = tmp_path / "manifest.db"
    manifest = DataManifest(manifest_db)

    recorder = Recorder(data_dir=data_dir, manifest=manifest, max_buffer_rows=100)

    t0 = datetime(2026, 11, 6, 9, 15, 0, tzinfo=UTC)
    # INFY tick at 09:15:00 has exch_ts 09:15:00 and seq 1
    recorder.on_market_event(_make_tick("NSE:EQ:INFY", t0, 100.0, 10), seq=1)
    # GOLD tick has exch_ts 09:13:45 (delayed exchange time) but arrived second (seq 2)
    recorder.on_market_event(
        _make_tick("MCX:FNO:GOLD", t0 - timedelta(seconds=75), 75000.0, 5), seq=2
    )
    # INFY tick 2 has seq 3
    recorder.on_market_event(_make_tick("NSE:EQ:INFY", t0 + timedelta(seconds=2), 101.0, 20), seq=3)

    recorder.close()

    files = [f.path for f in manifest.get_files()]
    feed = ReplayFeed(files=files)
    events_with_seq = list(feed.iter_with_seq())
    assert len(events_with_seq) == 3

    assert events_with_seq[0][1] == 1
    assert str(events_with_seq[0][0].instrument) == "NSE:EQ:INFY"
    assert events_with_seq[1][1] == 2
    assert str(events_with_seq[1][0].instrument) == "MCX:FNO:GOLD"
    assert events_with_seq[2][1] == 3
    assert str(events_with_seq[2][0].instrument) == "NSE:EQ:INFY"


def test_replay_feed_iter_with_seq_and_standard_iter(tmp_path: Path) -> None:
    """ReplayFeed supports both standard iteration and iteration with sequence numbers."""
    data_dir = tmp_path / "data"
    manifest_db = tmp_path / "manifest.db"
    manifest = DataManifest(manifest_db)

    rec = Recorder(data_dir=data_dir, manifest=manifest, max_buffer_rows=10)
    t0 = datetime(2026, 11, 6, 9, 15, 0, tzinfo=UTC)
    rec.on_market_event(_make_tick("NSE:EQ:TCS", t0, 3000.0, 10), seq=42)
    rec.close()

    files = [f.path for f in manifest.get_files()]
    feed = ReplayFeed(files=files)

    items = list(feed.iter_with_seq())
    assert len(items) == 1
    ev, seq = items[0]
    assert seq == 42
    assert ev.ltp == 3000.0

    feed_std = ReplayFeed(files=files)
    events = list(feed_std)
    assert len(events) == 1
    assert events[0].ltp == 3000.0


def test_bar_builder_drops_late_ticks_after_period_closed() -> None:
    """BarBuilder drops late ticks whose exchange time is before the last closed bar end."""
    closed_bars: list[Bar] = []
    late_events: list[SystemEvent] = []

    bus = EventBus()
    bus.subscribe(SystemEvent, lambda ev: late_events.append(ev))

    builder = BarBuilder(
        timeframes=["1m"],
        bus=bus,
        on_bar_closed=lambda b: closed_bars.append(b),
    )

    t0 = datetime(2026, 11, 6, 9, 15, 10, tzinfo=UTC)
    inst = "NSE:EQ:INFY"

    # Bar 09:15 - 09:16
    builder.on_tick(_make_tick(inst, t0, 100.0, 10))
    builder.on_tick(_make_tick(inst, t0 + timedelta(seconds=20), 102.0, 20))

    # Tick at 09:16:01 closes 09:15-09:16 bar
    builder.on_tick(_make_tick(inst, t0 + timedelta(seconds=51), 103.0, 30))
    assert len(closed_bars) == 1
    assert closed_bars[0].end_ts == datetime(2026, 11, 6, 9, 16, 0, tzinfo=UTC)

    # Late tick arriving with exch_ts 09:15:58 (belongs to closed bar)
    late_tick = _make_tick(inst, t0 + timedelta(seconds=48), 101.5, 25)
    builder.on_tick(late_tick)

    # Must be dropped without creating a duplicate/reopened 09:15-09:16 bar
    assert len(closed_bars) == 1
    assert builder.late_tick_count == 1
    assert any(e.kind == "feed.late_tick" for e in late_events)


def test_bar_builder_timer_close_callback_and_explicit_close() -> None:
    """BarBuilder notifies when a bar closes by timer and supports explicit replay closing."""
    closed_bars: list[Bar] = []
    timer_closes: list[tuple[Any, str, datetime]] = []

    builder = BarBuilder(
        timeframes=["1m"],
        on_bar_closed=lambda b: closed_bars.append(b),
        on_timer_bar_closed=lambda inst, tf, end: timer_closes.append((inst, tf, end)),
    )

    t0 = datetime(2026, 11, 6, 9, 15, 10, tzinfo=UTC)
    inst = "NSE:EQ:TCS"
    builder.on_tick(_make_tick(inst, t0, 3000.0, 10))
    assert len(closed_bars) == 0

    # Explicit close (simulating replay of live timer boundary close)
    bar_end = datetime(2026, 11, 6, 9, 16, 0, tzinfo=UTC)
    builder.close_bar_timer(inst, "1m", bar_end)

    assert len(closed_bars) == 1
    assert closed_bars[0].close == 3000.0
    assert closed_bars[0].end_ts == bar_end


def test_deterministic_bars_paper_and_replay_with_timer_jitter_and_late_ticks(
    tmp_path: Path,
) -> None:
    """Paper run with timer jitter and late ticks produces identical bars when replayed."""
    data_dir = tmp_path / "data"
    manifest_db = tmp_path / "manifest.db"
    manifest = DataManifest(manifest_db)
    recorder = Recorder(data_dir=data_dir, manifest=manifest, max_buffer_rows=100)

    # Live simulation:
    paper_bars: list[Bar] = []
    timer_events: list[dict[str, Any]] = []

    curr_seq = 0

    def _record_timer_close(inst: Any, tf: str, end: datetime) -> None:
        timer_events.append(
            {"instrument": str(inst), "timeframe": tf, "end_ts": end.isoformat(), "seq": curr_seq}
        )

    builder_paper = BarBuilder(
        timeframes=["1m"],
        on_bar_closed=lambda b: paper_bars.append(b),
        on_timer_bar_closed=_record_timer_close,
    )

    t0 = datetime(2026, 11, 6, 9, 15, 10, tzinfo=UTC)
    t_ticks = [
        _make_tick("NSE:EQ:INFY", t0, 100.0, 10),
        _make_tick("NSE:EQ:INFY", t0 + timedelta(seconds=20), 102.0, 20),
    ]

    for tick in t_ticks:
        curr_seq += 1
        recorder.on_market_event(tick, seq=curr_seq)
        builder_paper.on_tick(tick)

    # Timer boundary close occurs at tick seq 2
    period_end = datetime(2026, 11, 6, 9, 16, 0, tzinfo=UTC)
    builder_paper.close_bar_timer("NSE:EQ:INFY", "1m", period_end)
    timer_events.append(
        {
            "instrument": "NSE:EQ:INFY",
            "timeframe": "1m",
            "end_ts": period_end.isoformat(),
            "seq": curr_seq,
        }
    )

    # Late tick arrives after timer close
    curr_seq += 1
    late_tick = _make_tick("NSE:EQ:INFY", t0 + timedelta(seconds=45), 101.0, 25)
    recorder.on_market_event(late_tick, seq=curr_seq)
    builder_paper.on_tick(late_tick)

    # Tick in next bar
    curr_seq += 1
    next_tick = _make_tick("NSE:EQ:INFY", t0 + timedelta(seconds=70), 105.0, 40)
    recorder.on_market_event(next_tick, seq=curr_seq)
    builder_paper.on_tick(next_tick)

    recorder.close()

    # Replay:
    replay_bars: list[Bar] = []
    builder_replay = BarBuilder(
        timeframes=["1m"],
        on_bar_closed=lambda b: replay_bars.append(b),
    )

    files = [f.path for f in manifest.get_files()]
    feed = ReplayFeed(files=files)
    timer_closes_by_seq = {int(e["seq"]): e for e in timer_events}

    for ev, seq_num in feed.iter_with_seq():
        assert seq_num is not None
        if isinstance(ev, Tick):
            builder_replay.on_tick(ev)
        if seq_num in timer_closes_by_seq:
            tc = timer_closes_by_seq[seq_num]
            builder_replay.close_bar_timer(
                tc["instrument"], tc["timeframe"], datetime.fromisoformat(tc["end_ts"])
            )

    assert len(paper_bars) == len(replay_bars)
    for pb, rb in zip(paper_bars, replay_bars, strict=True):
        assert pb.open == rb.open
        assert pb.high == rb.high
        assert pb.low == rb.low
        assert pb.close == rb.close
        assert pb.volume == rb.volume
        assert pb.start_ts == rb.start_ts
        assert pb.end_ts == rb.end_ts


def test_fake_feed_graceful_stop_cancels_task_and_awaits_aclose(tmp_project: Path) -> None:
    """Clean Ctrl-C stop cancels feed task, awaits stream.aclose(), and closes recorder."""
    _seed_instrument_cache(tmp_project)
    aclose_called = False

    class _MockFeedStream:
        def __aiter__(self) -> _MockFeedStream:
            return self

        async def __anext__(self) -> tuple[Tick, int | None]:
            await asyncio.sleep(0.01)
            t = _make_tick("NSE:EQ:INFY", datetime.now(UTC), 100.0, 1)
            return (t, None)

        async def aclose(self) -> None:
            nonlocal aclose_called
            aclose_called = True

    with open_runtime(tmp_project, run="demo_paper") as rt:
        runner = SessionRunner(rt, mode="paper", dry_run=False)

        def _mock_build_event_stream(c: Any, clk: Any) -> Any:
            return _MockFeedStream()

        runner._build_event_stream = _mock_build_event_stream  # type: ignore[assignment]

        async def _run_and_interrupt() -> Path:
            run_task = asyncio.create_task(runner.run())
            await asyncio.sleep(0.05)
            runner.stop("interrupt")
            return await run_task

        res_path = asyncio.run(_run_and_interrupt())
        assert res_path.is_dir()
        assert aclose_called is True
        metrics = json.loads((res_path / "metrics.json").read_text(encoding="utf-8"))
        assert metrics["stop_reason"] == "interrupt"


def test_stop_interrupt_verdict_stopped_not_fail(tmp_project: Path) -> None:
    """An operator interrupt produces verdict STOPPED, not FAIL, and is_fail is False."""
    _seed_instrument_cache(tmp_project)
    with open_runtime(tmp_project, run="demo_paper") as rt:
        runner = SessionRunner(rt, mode="paper", dry_run=False)

        tick = _make_tick("NSE:EQ:INFY", datetime.now(UTC), 100.0, 1)
        runner._build_event_stream = lambda c, clk: _SingleListStream([(tick, None)])  # type: ignore[assignment]
        runner._interrupted = True
        res_path = asyncio.run(runner.run())

        metrics = json.loads((res_path / "metrics.json").read_text(encoding="utf-8"))
        session_text = (res_path / "session.md").read_text(encoding="utf-8")

        assert metrics["stop_reason"] == "interrupt"
        assert "Verdict: STOPPED" in session_text
        assert "Stop Reason: interrupt" in session_text


def test_stop_error_verdict_fail(tmp_project: Path) -> None:
    """Session failure due to unhandled error sets verdict FAIL and stop_reason error."""
    _seed_instrument_cache(tmp_project)
    with open_runtime(tmp_project, run="demo_paper") as rt:
        runner = SessionRunner(rt, mode="paper", dry_run=False)
        runner._run_error = "Broker network connection reset"
        runner._stop_event.set()

        runner._build_event_stream = lambda c, clk: _SingleListStream([])  # type: ignore[assignment]
        res_path = asyncio.run(runner.run())

        metrics = json.loads((res_path / "metrics.json").read_text(encoding="utf-8"))
        session_text = (res_path / "session.md").read_text(encoding="utf-8")
        assert metrics["stop_reason"] == "error"
        assert "Verdict: FAIL" in session_text


def test_stop_feed_halt_verdict_fail(tmp_project: Path) -> None:
    """Session terminated by feed halt / gap sets verdict FAIL and stop_reason feed halt."""
    _seed_instrument_cache(tmp_project)
    with open_runtime(tmp_project, run="demo_paper") as rt:
        runner = SessionRunner(rt, mode="paper", dry_run=False)
        runner._feed_halted = True
        runner._stop_event.set()

        runner._build_event_stream = lambda c, clk: _SingleListStream([])  # type: ignore[assignment]
        res_path = asyncio.run(runner.run())

        metrics = json.loads((res_path / "metrics.json").read_text(encoding="utf-8"))
        session_text = (res_path / "session.md").read_text(encoding="utf-8")
        assert metrics["stop_reason"] == "feed halt"
        assert "Verdict: FAIL" in session_text


def test_stop_killswitch_verdict_fail(tmp_project: Path) -> None:
    """Session terminated by kill switch breach sets verdict FAIL and stop_reason kill switch."""
    _seed_instrument_cache(tmp_project)
    with open_runtime(tmp_project, run="demo_paper") as rt:
        runner = SessionRunner(rt, mode="paper", dry_run=False)
        runner._killswitch_tripped = True
        runner._killswitch_events = 1
        runner._stop_event.set()

        runner._build_event_stream = lambda c, clk: _SingleListStream([])  # type: ignore[assignment]
        res_path = asyncio.run(runner.run())

        metrics = json.loads((res_path / "metrics.json").read_text(encoding="utf-8"))
        session_text = (res_path / "session.md").read_text(encoding="utf-8")
        assert metrics["stop_reason"] == "kill switch"
        assert "Verdict: FAIL" in session_text


def test_session_report_prints_stop_reason_interrupt(tmp_project: Path) -> None:
    """Session report CLI command prints Stop Reason: interrupt and Verdict: STOPPED."""
    _seed_instrument_cache(tmp_project)
    with open_runtime(tmp_project, run="demo_paper") as rt:
        runner = SessionRunner(rt, mode="paper", dry_run=False)
        runner._interrupted = True
        runner._stop_event.set()

        runner._build_event_stream = lambda c, clk: _SingleListStream([])  # type: ignore[assignment]
        res_path = asyncio.run(runner.run())

        cli_runner = CliRunner()
        res = cli_runner.invoke(
            app, ["--root", str(tmp_project), "session", "report", res_path.name]
        )
        assert res.exit_code == 0
        assert "Verdict:              STOPPED" in res.output
        assert "Stop Reason:          interrupt" in res.output


def test_session_report_prints_stop_reason_end_of_session(tmp_project: Path) -> None:
    """Session report CLI command prints Stop Reason: end of session on clean termination."""
    _seed_instrument_cache(tmp_project)
    with open_runtime(tmp_project, run="demo_paper") as rt:
        runner = SessionRunner(rt, mode="paper", dry_run=False)
        runner._stop_event.set()

        runner._build_event_stream = lambda c, clk: _SingleListStream([])  # type: ignore[assignment]
        res_path = asyncio.run(runner.run())

        cli_runner = CliRunner()
        res = cli_runner.invoke(
            app, ["--root", str(tmp_project), "session", "report", res_path.name]
        )
        assert res.exit_code == 0
        assert "Stop Reason:          end of session" in res.output


def test_results_compare_reports_differing_signal_with_ts_and_price(tmp_path: Path) -> None:
    """results compare prints both timestamp and price when signals differ."""
    dir_a = tmp_path / "results" / "run_a"
    dir_b = tmp_path / "results" / "run_b"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)

    sig_a = {
        "kind": "signal",
        "ts_utc": "2026-11-06T06:27:00+00:00",
        "payload_json": json.dumps(
            {
                "strategy": "sma_cross",
                "instrument": "NSE:EQ:HDFCBANK",
                "side": "BUY",
                "qty": 1,
                "intent_id": "entry_1",
                "price": "771.30",
                "ts": "2026-11-06T06:27:00+00:00",
            }
        ),
    }
    sig_b = {
        "kind": "signal",
        "ts_utc": "2026-11-06T06:26:00+00:00",
        "payload_json": json.dumps(
            {
                "strategy": "sma_cross",
                "instrument": "NSE:EQ:HDFCBANK",
                "side": "BUY",
                "qty": 1,
                "intent_id": "entry_2",
                "price": "770.70",
                "ts": "2026-11-06T06:26:00+00:00",
            }
        ),
    }

    (dir_a / "events.jsonl").write_text(json.dumps(sig_a) + "\n", encoding="utf-8")
    (dir_b / "events.jsonl").write_text(json.dumps(sig_b) + "\n", encoding="utf-8")
    (dir_a / "metrics.json").write_text("{}", encoding="utf-8")
    (dir_b / "metrics.json").write_text("{}", encoding="utf-8")

    is_equal, diff = compare_runs(tmp_path, "run_a", "run_b")
    assert is_equal is False
    assert diff is not None
    assert "signals[0]" in diff
    assert "2026-11-06T06:27:00+00:00" in diff
    assert "2026-11-06T06:26:00+00:00" in diff


def test_results_compare_reports_differing_fill_with_ts_and_price(tmp_path: Path) -> None:
    """results compare prints both timestamp and price when fills differ."""
    dir_a = tmp_path / "results" / "run_a"
    dir_b = tmp_path / "results" / "run_b"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)

    fill_a = {
        "kind": "fill",
        "ts_utc": "2026-11-06T06:38:00.66+00:00",
        "payload_json": json.dumps(
            {
                "instrument": "NSE:EQ:HDFCBANK",
                "side": "BUY",
                "qty": 1,
                "price": "771.15",
                "charges_total": "0.30",
                "ts": "2026-11-06T06:38:00.66+00:00",
            }
        ),
    }
    fill_b = {
        "kind": "fill",
        "ts_utc": "2026-11-06T06:37:00.38+00:00",
        "payload_json": json.dumps(
            {
                "instrument": "NSE:EQ:HDFCBANK",
                "side": "BUY",
                "qty": 1,
                "price": "771.30",
                "charges_total": "0.30",
                "ts": "2026-11-06T06:37:00.38+00:00",
            }
        ),
    }

    (dir_a / "events.jsonl").write_text(json.dumps(fill_a) + "\n", encoding="utf-8")
    (dir_b / "events.jsonl").write_text(json.dumps(fill_b) + "\n", encoding="utf-8")
    (dir_a / "metrics.json").write_text(json.dumps({"gross_pnl": -20.7}), encoding="utf-8")
    (dir_b / "metrics.json").write_text(json.dumps({"gross_pnl": -20.3}), encoding="utf-8")

    is_equal, diff = compare_runs(tmp_path, "run_a", "run_b")
    assert is_equal is False
    assert diff is not None
    assert "fills[0]" in diff
    assert "771.15" in diff
    assert "771.30" in diff
    assert "2026-11-06T06:38:00.66+00:00" in diff
    assert "2026-11-06T06:37:00.38+00:00" in diff


def test_results_compare_reports_differing_order_with_ts_and_price(tmp_path: Path) -> None:
    """results compare prints both timestamp and price when orders differ."""
    dir_a = tmp_path / "results" / "run_a"
    dir_b = tmp_path / "results" / "run_b"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)

    ord_a = {
        "kind": "order.created",
        "ts_utc": "2026-11-06T06:27:00+00:00",
        "payload_json": json.dumps(
            {
                "spec": {
                    "instrument": "NSE:EQ:INFY",
                    "side": "BUY",
                    "qty": 1,
                    "type": "LIMIT",
                    "price": "100.50",
                    "product": "MIS",
                },
                "created_ts": "2026-11-06T06:27:00+00:00",
            }
        ),
    }
    ord_b = {
        "kind": "order.created",
        "ts_utc": "2026-11-06T06:26:00+00:00",
        "payload_json": json.dumps(
            {
                "spec": {
                    "instrument": "NSE:EQ:INFY",
                    "side": "BUY",
                    "qty": 1,
                    "type": "LIMIT",
                    "price": "100.20",
                    "product": "MIS",
                },
                "created_ts": "2026-11-06T06:26:00+00:00",
            }
        ),
    }

    (dir_a / "events.jsonl").write_text(json.dumps(ord_a) + "\n", encoding="utf-8")
    (dir_b / "events.jsonl").write_text(json.dumps(ord_b) + "\n", encoding="utf-8")
    (dir_a / "metrics.json").write_text("{}", encoding="utf-8")
    (dir_b / "metrics.json").write_text("{}", encoding="utf-8")

    is_equal, diff = compare_runs(tmp_path, "run_a", "run_b")
    assert is_equal is False
    assert diff is not None
    assert "orders[0]" in diff
    assert "100.50" in diff
    assert "100.20" in diff


def test_results_compare_reports_count_mismatch_with_first_missing_item(tmp_path: Path) -> None:
    """results compare identifies count mismatches and identifies first missing event."""
    dir_a = tmp_path / "results" / "run_a"
    dir_b = tmp_path / "results" / "run_b"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)

    sig1 = {
        "kind": "signal",
        "ts_utc": "2026-11-06T06:25:00+00:00",
        "payload_json": json.dumps(
            {
                "strategy": "s",
                "instrument": "I",
                "side": "BUY",
                "qty": 1,
                "intent_id": "1",
                "price": "10",
            }
        ),
    }
    sig2 = {
        "kind": "signal",
        "ts_utc": "2026-11-06T06:26:00+00:00",
        "payload_json": json.dumps(
            {
                "strategy": "s",
                "instrument": "I",
                "side": "SELL",
                "qty": 1,
                "intent_id": "2",
                "price": "12",
            }
        ),
    }

    (dir_a / "events.jsonl").write_text(
        json.dumps(sig1) + "\n" + json.dumps(sig2) + "\n", encoding="utf-8"
    )
    (dir_b / "events.jsonl").write_text(json.dumps(sig1) + "\n", encoding="utf-8")
    (dir_a / "metrics.json").write_text("{}", encoding="utf-8")
    (dir_b / "metrics.json").write_text("{}", encoding="utf-8")

    is_equal, diff = compare_runs(tmp_path, "run_a", "run_b")
    assert is_equal is False
    assert diff is not None
    assert "signals count (2 != 1)" in diff


def test_results_compare_identical_runs_equal_true(tmp_path: Path) -> None:
    """Identical runs evaluate to equal: True without differences."""
    dir_a = tmp_path / "results" / "run_a"
    dir_b = tmp_path / "results" / "run_b"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)

    fill = {
        "kind": "fill",
        "ts_utc": "2026-11-06T06:38:00+00:00",
        "payload_json": json.dumps(
            {
                "instrument": "NSE:EQ:HDFCBANK",
                "side": "BUY",
                "qty": 1,
                "price": "771.15",
                "charges_total": "0.30",
            }
        ),
    }
    (dir_a / "events.jsonl").write_text(json.dumps(fill) + "\n", encoding="utf-8")
    (dir_b / "events.jsonl").write_text(json.dumps(fill) + "\n", encoding="utf-8")
    (dir_a / "metrics.json").write_text(json.dumps({"gross_pnl": 10.0}), encoding="utf-8")
    (dir_b / "metrics.json").write_text(json.dumps({"gross_pnl": 10.0}), encoding="utf-8")

    is_equal, diff = compare_runs(tmp_path, "run_a", "run_b")
    assert is_equal is True
    assert diff is None


def test_recorder_close_flushes_all_buffers(tmp_path: Path) -> None:
    """Calling recorder.close() cleanly flushes all buffered events to parquet."""
    data_dir = tmp_path / "data"
    manifest_db = tmp_path / "manifest.db"
    manifest = DataManifest(manifest_db)

    recorder = Recorder(data_dir=data_dir, manifest=manifest, max_buffer_rows=100)
    t0 = datetime(2026, 11, 6, 9, 15, 0, tzinfo=UTC)
    recorder.on_market_event(_make_tick("NSE:EQ:INFY", t0, 100.0, 10), seq=1)
    # File not yet flushed because max_buffer_rows is 100
    assert len(manifest.get_files()) == 0

    recorder.close()
    assert len(manifest.get_files()) == 1


def test_manifest_and_metrics_include_first_last_recv_utc(tmp_project: Path) -> None:
    """Metrics and manifest record first_tick_recv_utc and last_tick_recv_utc."""
    _seed_instrument_cache(tmp_project)
    with open_runtime(tmp_project, run="demo_paper") as rt:
        runner = SessionRunner(rt, mode="paper", dry_run=False)

        t_recv1 = datetime(2026, 11, 6, 9, 15, 0, 500000, tzinfo=UTC)
        t_recv2 = datetime(2026, 11, 6, 9, 16, 0, 500000, tzinfo=UTC)
        ticks = [
            _make_tick(
                "NSE:EQ:INFY",
                datetime(2026, 11, 6, 9, 15, 0, tzinfo=UTC),
                100.0,
                1,
                recv_ts=t_recv1,
            ),
            _make_tick(
                "NSE:EQ:INFY",
                datetime(2026, 11, 6, 9, 16, 0, tzinfo=UTC),
                101.0,
                2,
                recv_ts=t_recv2,
            ),
        ]

        runner._build_event_stream = lambda c, clk: _SingleListStream([(t, None) for t in ticks])  # type: ignore[assignment]
        res_path = asyncio.run(runner.run())

        metrics = json.loads((res_path / "metrics.json").read_text(encoding="utf-8"))
        manifest = json.loads((res_path / "manifest.json").read_text(encoding="utf-8"))

        assert metrics["first_tick_recv_utc"] == t_recv1.isoformat()
        assert metrics["last_tick_recv_utc"] == t_recv2.isoformat()
        assert manifest["first_tick_recv_utc"] == t_recv1.isoformat()
        assert manifest["last_tick_recv_utc"] == t_recv2.isoformat()


@settings(max_examples=25)
@given(
    st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=500),
            st.sampled_from(["NSE:EQ:INFY", "NSE:EQ:TCS", "NSE:EQ:HDFCBANK"]),
            st.floats(min_value=10.0, max_value=5000.0),
        ),
        min_size=2,
        max_size=30,
        unique_by=lambda x: x[0],
    )
)
def test_property_monotonic_sequence_ordering(items: list[tuple[int, str, float]]) -> None:
    """Property test: ReplayFeed merges across files in strictly increasing sequence order."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        data_dir = tmp_path / "data"
        manifest_db = tmp_path / "manifest.db"
        manifest = DataManifest(manifest_db)

        recorder = Recorder(data_dir=data_dir, manifest=manifest, max_buffer_rows=100)

        t0 = datetime(2026, 11, 6, 9, 15, 0, tzinfo=UTC)
        for seq_num, inst, price in sorted(items, key=lambda x: x[0]):
            tick = _make_tick(inst, t0 + timedelta(milliseconds=seq_num * 10), price, 1)
            recorder.on_market_event(tick, seq=seq_num)

        recorder.close()

        files = [f.path for f in manifest.get_files()]
        feed = ReplayFeed(files=files)
        replayed_seqs = [s for _, s in feed.iter_with_seq()]

        assert replayed_seqs == sorted(replayed_seqs)
        assert len(replayed_seqs) == len(items)


@settings(max_examples=25)
@given(
    st.lists(
        st.integers(min_value=0, max_value=300),
        min_size=3,
        max_size=40,
    )
)
def test_property_late_tick_rejection_invariance(deltas: list[int]) -> None:
    """Property test: BarBuilder never creates overlapping bars with out-of-order ticks."""
    closed_bars: list[Bar] = []
    builder = BarBuilder(
        timeframes=["1m"],
        on_bar_closed=lambda b: closed_bars.append(b),
    )

    t0 = datetime(2026, 11, 6, 9, 15, 0, tzinfo=UTC)
    inst = "NSE:EQ:INFY"

    for d in deltas:
        tick = _make_tick(inst, t0 + timedelta(seconds=d), 100.0, 10)
        builder.on_tick(tick)

    # Verify all emitted bars are strictly sequential without period overlap
    for i in range(len(closed_bars) - 1):
        assert closed_bars[i].end_ts <= closed_bars[i + 1].start_ts


def test_session_a_compare_verifies_first_difference() -> None:
    """results compare on Session A identifies the first differing signal with ts and price."""
    root = Path(__file__).resolve().parent.parent.parent.parent
    paper_dir = root / "results" / "demo_paper_fast_paper_20261106_053218_b945f8"
    replay_dir = root / "results" / "demo_paper_fast_replay_20261106_064442_8fc49e"

    if not paper_dir.is_dir() or not replay_dir.is_dir():
        pytest.skip("Session A results bundle not present")

    is_equal, diff = compare_runs(root, paper_dir.name, replay_dir.name)
    assert is_equal is False
    assert diff is not None
    assert "signals[31]" in diff
    assert "2026-11-06T06:27:00" in diff
    assert "2026-11-06T06:26:00" in diff
    assert "metrics.gross_pnl: -20.7 != -20.3" in diff
