"""Tests for Scheduler with SimClock and WallClock."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from algodesk.engine.clock import SimClock, WallClock
from algodesk.engine.scheduler import Scheduler


def test_scheduler_one_shot_and_cancel() -> None:
    t0 = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    clock = SimClock(t0)
    scheduler = Scheduler(clock)

    events: list[str] = []
    cb = lambda name: events.append(f"{name}_fired")  # noqa: E731
    scheduler.schedule_at("open", t0 + timedelta(seconds=10), cb)
    scheduler.schedule_at("heartbeat", t0 + timedelta(seconds=5), lambda: events.append("hb_fired"))

    assert scheduler.has_timer("open")
    assert scheduler.has_timer("heartbeat")
    assert scheduler.pending_count == 2

    # Advance to 5s: heartbeat fires
    fired = scheduler.advance_to(t0 + timedelta(seconds=5))
    assert fired == 1
    assert events == ["hb_fired"]
    assert not scheduler.has_timer("heartbeat")
    assert scheduler.has_timer("open")

    # Cancel open timer
    assert scheduler.cancel("open") is True
    assert scheduler.cancel("open") is False
    assert scheduler.pending_count == 0

    # Advance to 15s: nothing fires
    fired = scheduler.advance_to(t0 + timedelta(seconds=15))
    assert fired == 0
    assert events == ["hb_fired"]


def test_scheduler_deterministic_order() -> None:
    """When due times are identical, timers fire strictly in sequence order."""
    t0 = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    clock = SimClock(t0)
    scheduler = Scheduler(clock)

    fired_order: list[str] = []
    same_time = t0 + timedelta(seconds=10)
    scheduler.schedule_at("t1", same_time, lambda name: fired_order.append(name))
    scheduler.schedule_at("t2", same_time, lambda name: fired_order.append(name))
    scheduler.schedule_at("t3", same_time, lambda name: fired_order.append(name))

    scheduler.advance_to(same_time)
    assert fired_order == ["t1", "t2", "t3"]


def test_scheduler_repeating() -> None:
    t0 = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    clock = SimClock(t0)
    scheduler = Scheduler(clock)

    records: list[datetime] = []
    scheduler.schedule_repeating(
        "hb",
        start=t0 + timedelta(seconds=5),
        interval_seconds=5.0,
        callback=lambda: records.append(clock.now()),
    )

    # Advance 20 seconds
    scheduler.advance_to(t0 + timedelta(seconds=20))
    # Due at 5s, 10s, 15s, 20s
    assert len(records) == 4
    assert records == [
        t0 + timedelta(seconds=5),
        t0 + timedelta(seconds=10),
        t0 + timedelta(seconds=15),
        t0 + timedelta(seconds=20),
    ]
    assert scheduler.has_timer("hb")


def test_scheduler_schedule_after() -> None:
    t0 = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    clock = SimClock(t0)
    scheduler = Scheduler(clock)

    hits: list[str] = []
    scheduler.schedule_after("delayed", delay_seconds=12.0, callback=lambda: hits.append("done"))
    scheduler.advance_to(t0 + timedelta(seconds=10))
    assert hits == []
    scheduler.advance_to(t0 + timedelta(seconds=12))
    assert hits == ["done"]


def test_scheduler_validation() -> None:
    clock = SimClock()
    scheduler = Scheduler(clock)
    with pytest.raises(ValueError, match="cannot be empty"):
        scheduler.schedule_at("", clock.now(), lambda: None)
    with pytest.raises(ValueError, match="positive"):
        scheduler.schedule_repeating("rep", clock.now(), interval_seconds=0, callback=lambda: None)


def test_scheduler_wall_mode() -> None:
    async def _async_test() -> None:
        clock = WallClock()
        scheduler = Scheduler(clock)
        events: list[str] = []

        scheduler.schedule_after(
            "quick", delay_seconds=0.01, callback=lambda: events.append("quick")
        )
        stop_event = asyncio.Event()

        async def _stopper() -> None:
            await asyncio.sleep(0.05)
            stop_event.set()

        await asyncio.gather(scheduler.run(stop_event), _stopper())
        assert events == ["quick"]

    asyncio.run(_async_test())
