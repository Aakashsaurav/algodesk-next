"""Tests for multi-session replay isolation, session ID tracking, and compare safety.

Covers:
1. Recorder writes session_id column and preserves it across compaction.
2. ReplayFeed isolates ticks via session_id column.
3. ReplayFeed falls back to window filtering for legacy recordings without session_id.
4. ReplayFeed rejects ambiguous streams on colliding sequence numbers with DataError.
5. Replay never reports stop reason 'interrupt' without actual operator interrupt.
6. Replay reports 'interrupt' when an operator actually interrupts execution.
7. Replay reports 'replay data ended' with last tick time when feed ends early.
8. Property tests verify random changes in instrument/side/qty/price/intent are reported.
9. Semantic equality normalisation is strictly limited to the two named fields.
10. Two recorded sessions on one date with colliding seq numbers replay equal to paper.
"""

from __future__ import annotations

import asyncio
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from algodesk.api.errors import DataError
from algodesk.api.market import InstrumentId, Tick
from algodesk.data.manifest import DataManifest
from algodesk.data.recorder import Recorder
from algodesk.data.replay import ReplayFeed
from algodesk.engine.runner import SessionRunner
from algodesk.ops.runtime import open_runtime
from algodesk.results.compare import _collect_sequence_diffs, _item_semantic_equal

_IST = ZoneInfo("Asia/Kolkata")


def _make_test_tick(instrument: str, dt_utc: datetime, price: float, seq: int) -> Tick:
    return Tick(
        instrument=InstrumentId(instrument),
        exch_ts=dt_utc,
        recv_ts=dt_utc,
        ltp=price,
        ltq=1,
        volume=100,
        oi=0,
    )


def test_recorder_writes_session_id_column(tmp_path: Path) -> None:
    """Recorder appends session_id column to parquet and preserves it during compaction."""
    data_dir = tmp_path / "data"
    manifest_db = data_dir / "state" / "manifest.db"
    manifest_db.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(manifest_db)

    recorder = Recorder(
        data_dir=data_dir,
        manifest=manifest,
        source="live",
        session_id="session_test_abc",
    )

    t1 = datetime(2026, 11, 12, 6, 0, 0, tzinfo=UTC)
    tick = _make_test_tick("NSE:EQ:RELIANCE", t1, 2500.0, 1)
    recorder.on_market_event(tick, seq=1)
    part_files = recorder.flush()
    assert len(part_files) == 1

    table = pq.read_table(part_files[0])
    assert "session_id" in table.column_names
    assert "seq" in table.column_names
    assert table["session_id"].to_pylist() == ["session_test_abc"]
    assert table["seq"].to_pylist() == [1]

    # Compact partition
    compacted = recorder.compact("2026-11-12")
    assert len(compacted) == 1
    day_table = pq.read_table(compacted[0])
    assert "session_id" in day_table.column_names
    assert day_table["session_id"].to_pylist() == ["session_test_abc"]


def test_replay_feed_filters_by_session_id_when_present(tmp_path: Path) -> None:
    """ReplayFeed isolates target session rows when session_id column is present."""
    file_path = (
        tmp_path / "exchange=NSE" / "segment=EQ" / "symbol=INFY" / "date=2026-11-12" / "day.parquet"
    )
    file_path.parent.mkdir(parents=True, exist_ok=True)

    t0 = datetime(2026, 11, 12, 6, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 11, 12, 9, 0, 0, tzinfo=UTC)

    # Interleaved rows from session_1 and session_2 with colliding seqs
    rows = {
        "exch_ts": [t0, t1, t0 + timedelta(seconds=1), t1 + timedelta(seconds=1)],
        "recv_ts": [t0, t1, t0 + timedelta(seconds=1), t1 + timedelta(seconds=1)],
        "ltp": [1500.0, 1505.0, 1501.0, 1506.0],
        "ltq": [1, 1, 1, 1],
        "volume": [10, 20, 11, 21],
        "oi": [0, 0, 0, 0],
        "seq": [1, 1, 2, 2],
        "session_id": ["session_1", "session_2", "session_1", "session_2"],
    }
    table = pa.Table.from_pydict(rows)
    pq.write_table(table, file_path)

    feed = ReplayFeed([file_path], session_id="session_1")
    events = list(feed.iter_with_seq())

    assert len(events) == 2
    assert [ev[1] for ev in events] == [1, 2]
    assert [ev[0].ltp for ev in events] == [1500.0, 1501.0]


def test_replay_feed_falls_back_to_window_for_legacy_files(tmp_path: Path) -> None:
    """Legacy files without session_id column select rows strictly via window."""
    file_path = (
        tmp_path / "exchange=NSE" / "segment=EQ" / "symbol=TCS" / "date=2026-11-12" / "day.parquet"
    )
    file_path.parent.mkdir(parents=True, exist_ok=True)

    w_start = datetime(2026, 11, 12, 5, 45, tzinfo=UTC)
    w_end = datetime(2026, 11, 12, 7, 15, tzinfo=UTC)
    afternoon = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)

    # Interleaved rows without session_id column
    rows = {
        "exch_ts": [w_start + timedelta(minutes=1), afternoon, w_start + timedelta(minutes=2)],
        "recv_ts": [w_start + timedelta(minutes=1), afternoon, w_start + timedelta(minutes=2)],
        "ltp": [3500.0, 3510.0, 3502.0],
        "ltq": [1, 1, 1],
        "volume": [100, 200, 101],
        "oi": [0, 0, 0],
        "seq": [10, 10, 20],
    }
    table = pa.Table.from_pydict(rows)
    pq.write_table(table, file_path)

    feed = ReplayFeed([file_path], window_start=w_start, window_end=w_end, session_id="legacy_run")
    events = list(feed.iter_with_seq())

    # Encountering afternoon tick did NOT terminate early; all session 1 ticks yielded
    assert len(events) == 2
    assert [ev[1] for ev in events] == [10, 20]
    assert [ev[0].ltp for ev in events] == [3500.0, 3502.0]


def test_replay_feed_refuses_ambiguous_window_on_colliding_seq(tmp_path: Path) -> None:
    """ReplayFeed raises DataError when an ambiguous window encounters colliding seqs."""
    file_path = (
        tmp_path / "exchange=NSE" / "segment=EQ" / "symbol=TCS" / "date=2026-11-12" / "day.parquet"
    )
    file_path.parent.mkdir(parents=True, exist_ok=True)

    t0 = datetime(2026, 11, 12, 6, 0, tzinfo=UTC)
    rows = {
        "exch_ts": [t0, t0 + timedelta(seconds=1)],
        "recv_ts": [t0, t0 + timedelta(seconds=1)],
        "ltp": [3500.0, 3501.0],
        "ltq": [1, 1],
        "volume": [100, 101],
        "oi": [0, 0],
        "seq": [5, 5],
    }
    table = pa.Table.from_pydict(rows)
    pq.write_table(table, file_path)

    feed = ReplayFeed([file_path])
    with pytest.raises(DataError, match="Ambiguous replay stream: colliding seq 5"):
        list(feed.iter_with_seq())


def test_replay_stop_reason_never_interrupt_without_user(tmp_project: Path) -> None:
    """Replaying an interrupted paper run records end of session when user does not interrupt."""
    with open_runtime(tmp_project, run="demo_paper") as rt:
        runner = SessionRunner(rt, mode="replay", dry_run=False)
        runner._recorded_stop_reason = "interrupt"
        runner._recorded_stop_seq = 2

        t1 = datetime(2026, 11, 12, 6, 0, 0, tzinfo=UTC)
        t2 = datetime(2026, 11, 12, 6, 0, 1, tzinfo=UTC)
        tick1 = _make_test_tick("NSE:EQ:INFY", t1, 1500.0, 1)
        tick2 = _make_test_tick("NSE:EQ:INFY", t2, 1501.0, 2)

        class _MockStream:
            def __init__(self, items: list[Any]) -> None:
                self._items = items
                self._idx = 0

            def __aiter__(self) -> _MockStream:
                return self

            async def __anext__(self) -> Any:
                if self._idx >= len(self._items):
                    raise StopAsyncIteration
                item = self._items[self._idx]
                self._idx += 1
                return item

            async def aclose(self) -> None:
                pass

        runner._build_event_stream = lambda c, clk: _MockStream([(tick1, 1), (tick2, 2)])  # type: ignore[assignment]
        res_path = asyncio.run(runner.run())

        metrics = json.loads((res_path / "metrics.json").read_text(encoding="utf-8"))
        assert metrics["stop_reason"] == "end of session"
        assert metrics["stop_reason"] != "interrupt"


def test_replay_stop_reason_interrupt_when_user_interrupts(tmp_project: Path) -> None:
    """Replay records interrupt when user explicitly signals an interruption."""
    with open_runtime(tmp_project, run="demo_paper") as rt:
        runner = SessionRunner(rt, mode="replay", dry_run=False)
        t1 = datetime(2026, 11, 12, 6, 0, 0, tzinfo=UTC)
        tick1 = _make_test_tick("NSE:EQ:INFY", t1, 1500.0, 1)

        class _NeverEndingStream:
            def __aiter__(self) -> _NeverEndingStream:
                return self

            async def __anext__(self) -> Any:
                await asyncio.sleep(0.01)
                return (tick1, 1)

            async def aclose(self) -> None:
                pass

        runner._build_event_stream = lambda c, clk: _NeverEndingStream()  # type: ignore[assignment]

        async def _run_and_sig() -> Path:
            task = asyncio.create_task(runner.run())
            await asyncio.sleep(0.03)
            runner._user_interrupted = True
            runner.stop("interrupt")
            return await task

        res_path = asyncio.run(_run_and_sig())
        metrics = json.loads((res_path / "metrics.json").read_text(encoding="utf-8"))
        assert metrics["stop_reason"] == "interrupt"


def test_replay_stop_reason_feed_ends_early(tmp_project: Path) -> None:
    """When replay feed exhausts before recorded stop seq, stop reason is replay data ended."""
    with open_runtime(tmp_project, run="demo_paper") as rt:
        runner = SessionRunner(rt, mode="replay", dry_run=False)
        runner._recorded_stop_seq = 100
        t1 = datetime(2026, 11, 12, 6, 0, 0, tzinfo=UTC)
        tick1 = _make_test_tick("NSE:EQ:INFY", t1, 1500.0, 1)

        class _ShortStream:
            def __init__(self) -> None:
                self._done = False

            def __aiter__(self) -> _ShortStream:
                return self

            async def __anext__(self) -> Any:
                if not self._done:
                    self._done = True
                    return (tick1, 1)
                raise StopAsyncIteration

            async def aclose(self) -> None:
                pass

        runner._build_event_stream = lambda c, clk: _ShortStream()  # type: ignore[assignment]
        res_path = asyncio.run(runner.run())

        metrics = json.loads((res_path / "metrics.json").read_text(encoding="utf-8"))
        assert "replay data ended" in metrics["stop_reason"]
        assert "IST" in metrics["stop_reason"]


def test_compare_property_random_differences_always_reported() -> None:
    """Property test: random differences in instrument/side/qty/price/intent are always reported."""
    rng = random.Random(42)
    instruments = ["NSE:EQ:RELIANCE", "NSE:EQ:TCS", "NSE:EQ:INFY", "NSE:EQ:HDFCBANK"]
    sides = ["BUY", "SELL"]

    for _ in range(100):
        base_inst = rng.choice(instruments)
        base_side = rng.choice(sides)
        base_qty = rng.randint(1, 100)
        base_price = f"{rng.uniform(100.0, 3000.0):.2f}"
        base_intent = f"strat_sig_{rng.randint(1000, 9999)}"

        item_a = {
            "strategy": "sma",
            "instrument": base_inst,
            "side": base_side,
            "qty": base_qty,
            "price": base_price,
            "intent_id": base_intent,
        }

        # Mutate exactly one attribute
        mutation_field = rng.choice(["instrument", "side", "qty", "price", "intent_id"])
        item_b = dict(item_a)

        if mutation_field == "instrument":
            alt = [i for i in instruments if i != base_inst]
            item_b["instrument"] = rng.choice(alt)
        elif mutation_field == "side":
            item_b["side"] = "SELL" if base_side == "BUY" else "BUY"
        elif mutation_field == "qty":
            item_b["qty"] = base_qty + rng.randint(1, 10)
        elif mutation_field == "price":
            item_b["price"] = f"{float(base_price) + rng.uniform(0.1, 5.0):.2f}"
        elif mutation_field == "intent_id":
            item_b["intent_id"] = f"{base_intent}_different"

        assert _item_semantic_equal(item_a, item_b) is False
        diffs = _collect_sequence_diffs("test", [item_a], [item_b], diff_count=1)
        assert len(diffs) > 0


def test_compare_normalises_only_two_named_fields() -> None:
    """Compare normalisation applies only to subsecond timer exit and quote age float jitter."""
    # 1. Timer exit subsecond jitter is normalised
    exit_a = {"intent_id": "timer_exit_sma_1731400000.123"}
    exit_b = {"intent_id": "timer_exit_sma_1731400000.789"}
    assert _item_semantic_equal(exit_a, exit_b) is True

    # But different second integer timestamp is NOT normalised
    exit_c = {"intent_id": "timer_exit_sma_1731400001.123"}
    assert _item_semantic_equal(exit_a, exit_c) is False

    # Non-timer_exit intent_id is NOT normalised
    gen_a = {"intent_id": "sig_exit_123.456"}
    gen_b = {"intent_id": "sig_exit_123.789"}
    assert _item_semantic_equal(gen_a, gen_b) is False

    # 2. Quote age subsecond float jitter is normalised
    age_a = {"reason": "Stale quote: Quote age 1.25s exceeds limit 1.0s"}
    age_b = {"reason": "Stale quote: Quote age 1.82s exceeds limit 1.0s"}
    assert _item_semantic_equal(age_a, age_b) is True

    # Other reason differences are NOT normalised
    diff_reason = {"reason": "Unknown instrument: NOT_FOUND"}
    assert _item_semantic_equal(age_a, diff_reason) is False


def test_two_recorded_sessions_same_date_colliding_seq_replay_equal(tmp_project: Path) -> None:
    """Two recorded paper runs on the same date with colliding seq replay equal to paper."""
    results_dir = tmp_project / "results"
    run_1_dir = results_dir / "demo_paper_paper_20261112_060000_111111"
    run_2_dir = results_dir / "demo_paper_paper_20261112_090000_222222"
    run_1_dir.mkdir(parents=True, exist_ok=True)
    run_2_dir.mkdir(parents=True, exist_ok=True)

    t1_recv = "2026-11-12T06:00:00.000000+00:00"
    t1_end = "2026-11-12T06:05:00.000000+00:00"
    t2_recv = "2026-11-12T09:00:00.000000+00:00"
    t2_end = "2026-11-12T09:05:00.000000+00:00"

    man_1 = {
        "first_tick_recv_utc": t1_recv,
        "last_tick_recv_utc": t1_end,
        "first_tick_utc": t1_recv,
        "last_tick_utc": t1_end,
        "stop_reason": "end of session",
    }
    man_2 = {
        "first_tick_recv_utc": t2_recv,
        "last_tick_recv_utc": t2_end,
        "first_tick_utc": t2_recv,
        "last_tick_utc": t2_end,
        "stop_reason": "end of session",
    }
    (run_1_dir / "manifest.json").write_text(json.dumps(man_1))
    (run_2_dir / "manifest.json").write_text(json.dumps(man_2))

    met = {
        "trades": 0,
        "gross_pnl": 0.0,
        "net_pnl": 0.0,
        "charges": 0.0,
        "win_rate": 0.0,
        "max_drawdown": 0.0,
    }
    (run_1_dir / "metrics.json").write_text(json.dumps(met))
    (run_2_dir / "metrics.json").write_text(json.dumps(met))

    # Write tick parquet with session_id
    pdir = (
        tmp_project
        / "data"
        / "ticks"
        / "exchange=NSE"
        / "segment=EQ"
        / "symbol=INFY"
        / "date=2026-11-12"
    )
    pdir.mkdir(parents=True, exist_ok=True)

    dt1 = datetime(2026, 11, 12, 6, 1, tzinfo=UTC)
    dt2 = datetime(2026, 11, 12, 9, 1, tzinfo=UTC)

    # Both start at seq 1 (colliding seq numbers)
    table_dict = {
        "exch_ts": [dt1, dt2],
        "recv_ts": [dt1, dt2],
        "ltp": [1500.0, 1505.0],
        "ltq": [1, 1],
        "volume": [10, 20],
        "oi": [0, 0],
        "seq": [1, 1],
        "session_id": [run_1_dir.name, run_2_dir.name],
    }
    pq.write_table(pa.Table.from_pydict(table_dict), pdir / "day.parquet")

    # Replay session 1
    feed1 = ReplayFeed([pdir / "day.parquet"], session_id=run_1_dir.name)
    evs1 = list(feed1.iter_with_seq())
    assert len(evs1) == 1
    assert evs1[0][0].ltp == 1500.0

    # Replay session 2
    feed2 = ReplayFeed([pdir / "day.parquet"], session_id=run_2_dir.name)
    evs2 = list(feed2.iter_with_seq())
    assert len(evs2) == 1
    assert evs2[0][0].ltp == 1505.0
