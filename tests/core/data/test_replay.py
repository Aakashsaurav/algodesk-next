"""Tests for ReplayFeed: k-way merge ordering, bounded memory batching, SimClock driving."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from algodesk.api.market import InstrumentId, Tick
from algodesk.data.manifest import DataManifest
from algodesk.data.recorder import Recorder
from algodesk.data.replay import ReplayFeed
from algodesk.engine.clock import SimClock


def test_replay_feed_kway_merge_and_clock_driving(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    manifest_db = tmp_path / "manifest.db"
    manifest = DataManifest(manifest_db)

    recorder = Recorder(data_dir=data_dir, manifest=manifest, max_buffer_rows=100)

    t0 = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    inst1 = "NSE:EQ:INFY"
    inst2 = "NSE:EQ:SBIN"

    # Instrument 1 events
    t1_1 = Tick(
        instrument=InstrumentId(inst1),
        exch_ts=t0,
        recv_ts=t0 + timedelta(milliseconds=10),
        ltp=1500.0,
        ltq=1,
        volume=10,
    )
    t1_2 = Tick(
        instrument=InstrumentId(inst1),
        exch_ts=t0 + timedelta(seconds=2),
        recv_ts=t0 + timedelta(seconds=2, milliseconds=10),
        ltp=1502.0,
        ltq=1,
        volume=20,
    )

    # Instrument 2 events interleaved
    t2_1 = Tick(
        instrument=InstrumentId(inst2),
        exch_ts=t0 + timedelta(seconds=1),
        recv_ts=t0 + timedelta(seconds=1, milliseconds=10),
        ltp=600.0,
        ltq=1,
        volume=15,
    )
    t2_2 = Tick(
        instrument=InstrumentId(inst2),
        exch_ts=t0 + timedelta(seconds=3),
        recv_ts=t0 + timedelta(seconds=3, milliseconds=10),
        ltp=601.0,
        ltq=1,
        volume=30,
    )

    recorder.on_market_event(t1_1)
    recorder.on_market_event(t1_2)
    recorder.on_market_event(t2_1)
    recorder.on_market_event(t2_2)

    files = recorder.flush()
    assert len(files) == 2

    clock = SimClock(t0)
    feed = ReplayFeed(files=files, clock=clock, batch_size=2)

    replayed_events = list(feed)
    assert len(replayed_events) == 4

    # Verify interleaved order strictly by (recv_ts, exch_ts, instrument)
    expected_order = [t1_1, t2_1, t1_2, t2_2]
    for actual, expected in zip(replayed_events, expected_order, strict=True):
        assert actual.instrument == expected.instrument
        assert actual.exch_ts == expected.exch_ts
        assert actual.recv_ts == expected.recv_ts
        assert actual.ltp == expected.ltp

    # Clock was driven to the latest event's recv_ts
    assert clock.now() == t2_2.recv_ts
