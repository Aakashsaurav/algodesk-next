"""Tests for BarBuilder: boundaries, grace timer, volume, late ticks, flat bars."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from algodesk.api.market import Bar, InstrumentId, Tick
from algodesk.engine.bars import BarBuilder, parse_timeframe_seconds
from algodesk.engine.clock import SimClock
from algodesk.engine.events import SystemEvent
from algodesk.engine.scheduler import Scheduler


def _make_tick(
    inst: str,
    ts: datetime,
    ltp: float,
    cum_vol: float,
) -> Tick:
    return Tick(
        instrument=InstrumentId(inst),
        exch_ts=ts,
        recv_ts=ts + timedelta(milliseconds=10),
        ltp=ltp,
        ltq=10,
        volume=cum_vol,
    )


def test_timeframe_parsing() -> None:
    assert parse_timeframe_seconds("30s") == 30
    assert parse_timeframe_seconds("1m") == 60
    assert parse_timeframe_seconds("5m") == 300
    assert parse_timeframe_seconds("1h") == 3600
    assert parse_timeframe_seconds("1d") == 86400


def test_bar_builder_boundary_and_next_period_close() -> None:
    closed_bars: list[Bar] = []
    builder = BarBuilder(
        timeframes=["1m"],
        on_bar_closed=lambda b: closed_bars.append(b),
    )

    t0 = datetime(2026, 10, 26, 9, 15, 10, tzinfo=UTC)
    inst = "NSE:EQ:INFY"

    # Tick 1 at 09:15:10
    builder.on_tick(_make_tick(inst, t0, 100.0, 50.0))
    assert len(closed_bars) == 0

    # Tick 2 at 09:15:40
    builder.on_tick(_make_tick(inst, t0 + timedelta(seconds=30), 105.0, 120.0))
    assert len(closed_bars) == 0

    # Tick 3 at 09:16:05 (next 1m period starts at 09:16:00) -> closes bar [09:15, 09:16)
    builder.on_tick(_make_tick(inst, t0 + timedelta(seconds=55), 102.0, 200.0))
    assert len(closed_bars) == 1
    bar = closed_bars[0]
    assert bar.open == 100.0
    assert bar.high == 105.0
    assert bar.low == 100.0
    assert bar.close == 105.0
    assert bar.volume == 120
    assert bar.complete is True
    assert bar.synthetic is False
    assert bar.start_ts == datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    assert bar.end_ts == datetime(2026, 10, 26, 9, 16, 0, tzinfo=UTC)


def test_bar_builder_volume_resets_and_deltas() -> None:
    closed_bars: list[Bar] = []
    builder = BarBuilder(
        timeframes=["1m"],
        on_bar_closed=lambda b: closed_bars.append(b),
    )

    t0 = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    inst = "NSE:EQ:INFY"

    # Day starts with cumulative volume 500
    builder.on_tick(_make_tick(inst, t0, 100.0, 500.0))
    builder.on_tick(_make_tick(inst, t0 + timedelta(seconds=30), 101.0, 650.0))

    # Next minute starts -> bar 1 closes with volume 650
    builder.on_tick(_make_tick(inst, t0 + timedelta(seconds=60), 102.0, 800.0))
    assert closed_bars[0].volume == 650

    # Feed reset occurs (volume drops from 800 to 50)
    builder.on_tick(_make_tick(inst, t0 + timedelta(seconds=70), 103.0, 50.0))
    # Close bar 2
    builder.on_tick(_make_tick(inst, t0 + timedelta(seconds=120), 104.0, 90.0))
    # Bar 2 volume: delta from 650 to 800 (150) + reset delta 50 = 200
    assert closed_bars[1].volume == 200


def test_bar_builder_late_ticks_dropped_and_journaled() -> None:
    system_events: list[SystemEvent] = []
    builder = BarBuilder(
        timeframes=["1m"],
        on_system_event=lambda e: system_events.append(e),
    )

    t0 = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    inst = "NSE:EQ:INFY"

    builder.on_tick(_make_tick(inst, t0 + timedelta(seconds=10), 100.0, 10.0))
    builder.on_tick(_make_tick(inst, t0 + timedelta(seconds=70), 102.0, 20.0))

    # Late tick arrives with exch_ts = 09:15:05 (belongs to already closed bar)
    late_tick = _make_tick(inst, t0 + timedelta(seconds=5), 99.0, 25.0)
    builder.on_tick(late_tick)

    assert builder.late_tick_count == 1
    assert len(system_events) == 1
    assert system_events[0].kind == "feed.late_tick"


def test_bar_builder_boundary_timer_grace_close() -> None:
    clock = SimClock(datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC))
    scheduler = Scheduler(clock)
    closed_bars: list[Bar] = []

    builder = BarBuilder(
        timeframes=["1m"],
        scheduler=scheduler,
        bar_close_grace_ms=500,
        on_bar_closed=lambda b: closed_bars.append(b),
    )

    t0 = datetime(2026, 10, 26, 9, 15, 20, tzinfo=UTC)
    inst = "NSE:EQ:INFY"
    builder.on_tick(_make_tick(inst, t0, 100.0, 10.0))

    # No more ticks arrive. Advance scheduler past boundary (09:16:00) + 500ms grace
    scheduler.advance_to(datetime(2026, 10, 26, 9, 16, 0, 500000, tzinfo=UTC))
    assert len(closed_bars) == 1
    assert closed_bars[0].complete is True
    assert closed_bars[0].end_ts == datetime(2026, 10, 26, 9, 16, 0, tzinfo=UTC)


def test_bar_builder_empty_period_flat_bars() -> None:
    closed_bars: list[Bar] = []
    builder = BarBuilder(
        timeframes=["1m"],
        fill_empty_bars=True,
        on_bar_closed=lambda b: closed_bars.append(b),
    )

    t0 = datetime(2026, 10, 26, 9, 15, 10, tzinfo=UTC)
    inst = "NSE:EQ:INFY"
    builder.on_tick(_make_tick(inst, t0, 100.0, 10.0))

    # Next tick arrives at 09:18:10 (skipping 09:16 and 09:17)
    builder.on_tick(_make_tick(inst, t0 + timedelta(minutes=3), 105.0, 20.0))

    # Expected: bar 09:15-09:16 real, bar 09:16-09:17 synthetic flat, bar 09:17-09:18 synthetic flat
    assert len(closed_bars) == 3
    assert closed_bars[0].synthetic is False
    assert closed_bars[1].synthetic is True
    assert closed_bars[1].open == 100.0
    assert closed_bars[1].close == 100.0
    assert closed_bars[1].volume == 0
    assert closed_bars[2].synthetic is True
