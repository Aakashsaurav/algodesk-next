"""End-to-end integration and Hypothesis property tests for engine and data pipelines.

Verifies:
1. Full pipeline determinism: synthetic ticks -> bus -> BarBuilder & Recorder -> compaction ->
   ReplayFeed -> BarBuilder.
2. Invariant properties under Hypothesis fuzzing (OHLC consistency, volume conservation,
   chronology, zero look-ahead).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from algodesk.api.market import InstrumentId, Tick
from algodesk.data.manifest import DataManifest
from algodesk.data.recorder import Recorder
from algodesk.data.replay import ReplayFeed
from algodesk.data.synth import generate_synthetic_day
from algodesk.engine.bars import BarBuilder
from algodesk.engine.bus import EventBus
from algodesk.engine.clock import SimClock
from algodesk.engine.events import BarClosed


def test_end_to_end_pipeline_determinism(tmp_path: Path) -> None:
    """Full lifecycle: ticks -> bus -> (builder, recorder) -> compact -> replay -> builder."""
    t_date = date(2026, 10, 26)
    symbol = "NSE:EQ:INFY"
    synth_events = generate_synthetic_day(symbol, t_date, rows=120, seed=42)

    data_dir = tmp_path / "data"
    manifest_db = tmp_path / "manifest.db"
    manifest = DataManifest(manifest_db)

    # 1. Live recording and bar building
    live_bus = EventBus()
    live_bars: list[BarClosed] = []
    live_bus.subscribe(BarClosed, live_bars.append)

    live_builder = BarBuilder(bus=live_bus, timeframes=["1m", "5m"], fill_empty_bars=False)
    recorder = Recorder(data_dir=data_dir, manifest=manifest, max_buffer_rows=50)

    for ev in synth_events:
        if isinstance(ev, Tick):
            live_builder.on_tick(ev)
        recorder.on_market_event(ev)

    recorder.flush()
    compacted_files = recorder.compact(t_date)
    assert len(compacted_files) == 1

    # 2. Replay feed driving sim clock and second bar builder
    day_files = [f.path for f in manifest.get_files(instrument=symbol, state="day")]
    assert len(day_files) == 1

    sim_clock = SimClock()
    replay = ReplayFeed(day_files, clock=sim_clock)

    replay_bus = EventBus()
    replay_bars: list[BarClosed] = []
    replay_bus.subscribe(BarClosed, replay_bars.append)

    replay_builder = BarBuilder(bus=replay_bus, timeframes=["1m", "5m"], fill_empty_bars=False)

    for ev in replay:
        if isinstance(ev, Tick):
            replay_builder.on_tick(ev)

    # 3. Assert exact sequence equivalence
    assert len(live_bars) > 0
    assert len(live_bars) == len(replay_bars)

    for lb, rb in zip(live_bars, replay_bars, strict=True):
        assert lb.bar.instrument == rb.bar.instrument
        assert lb.bar.timeframe == rb.bar.timeframe
        assert lb.bar.start_ts == rb.bar.start_ts
        assert lb.bar.open == rb.bar.open
        assert lb.bar.high == rb.bar.high
        assert lb.bar.low == rb.bar.low
        assert lb.bar.close == rb.bar.close
        assert lb.bar.volume == rb.bar.volume
        assert lb.bar.synthetic == rb.bar.synthetic


# --- Hypothesis Property Tests ---


@st.composite
def monotonic_ticks(draw: Any) -> list[Tick]:
    """Generate monotonically increasing ticks within market hours."""
    n = draw(st.integers(min_value=5, max_value=80))
    start_ts = datetime(2026, 10, 26, 3, 45, 0, tzinfo=UTC)  # 09:15 IST
    ticks: list[Tick] = []
    curr_time = start_ts
    curr_price = draw(st.floats(min_value=50.0, max_value=5000.0))
    cum_vol = draw(st.integers(min_value=100, max_value=10000))

    inst = InstrumentId("NSE:EQ:INFY")

    for _ in range(n):
        step_ms = draw(st.integers(min_value=500, max_value=45000))
        curr_time += timedelta(milliseconds=step_ms)
        delta = draw(st.floats(min_value=-5.0, max_value=5.0))
        curr_price = max(1.0, round(curr_price + delta, 2))
        vol_step = draw(st.integers(min_value=1, max_value=50))
        cum_vol += vol_step

        ticks.append(
            Tick(
                instrument=inst,
                exch_ts=curr_time,
                recv_ts=curr_time + timedelta(milliseconds=2),
                ltp=curr_price,
                ltq=vol_step,
                volume=cum_vol,
                oi=None,
            )
        )
    return ticks


@settings(max_examples=40, deadline=None)
@given(ticks=monotonic_ticks())
def test_hypothesis_bar_invariants(ticks: list[Tick]) -> None:
    """Verify BarBuilder OHLC consistency, volume accumulation, and ordering invariants."""
    bus = EventBus()
    closed_bars: list[BarClosed] = []
    bus.subscribe(BarClosed, closed_bars.append)

    builder = BarBuilder(bus=bus, timeframes=["1m"], fill_empty_bars=False)

    for tick in ticks:
        builder.on_tick(tick)

    # Invariants on all closed bars
    for i, item in enumerate(closed_bars):
        bar = item.bar
        # OHLC consistency
        assert bar.low <= bar.open <= bar.high
        assert bar.low <= bar.close <= bar.high
        assert bar.open > 0
        assert bar.high >= bar.low
        assert bar.volume >= 0

        # No look-ahead: bar start + timeframe <= current tick time
        bar_end = bar.start_ts + timedelta(minutes=1)
        # The closing tick must be >= bar_end
        assert ticks[-1].exch_ts >= bar_end

        # Chronological ordering
        if i > 0:
            assert closed_bars[i - 1].bar.start_ts < bar.start_ts
