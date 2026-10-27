"""Tests for WallClock and SimClock."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from algodesk.api.conformance import clock_problems
from algodesk.engine.clock import SimClock, WallClock


def test_wall_clock_conformance() -> None:
    clock = WallClock()
    assert clock_problems(clock) == []
    t1 = clock.now()
    assert t1.tzinfo == UTC
    ns1 = clock.monotonic_ns()
    time.sleep(0.005)
    ns2 = clock.monotonic_ns()
    assert ns2 >= ns1


def test_sim_clock_conformance() -> None:
    t0 = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    clock = SimClock(initial_time=t0)
    assert clock_problems(clock) == []
    assert clock.now() == t0
    assert clock.monotonic_ns() == 0


def test_sim_clock_advancement() -> None:
    t0 = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    clock = SimClock(t0)
    t1 = t0 + timedelta(seconds=10)
    clock.set_time(t1)
    assert clock.now() == t1
    assert clock.monotonic_ns() == 10_000_000_000

    # Moving forward again
    t2 = t1 + timedelta(seconds=5)
    clock.set_time(t2)
    assert clock.now() == t2
    assert clock.monotonic_ns() == 15_000_000_000


def test_sim_clock_cannot_move_backwards() -> None:
    t0 = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    clock = SimClock(t0)
    past = t0 - timedelta(seconds=1)
    with pytest.raises(ValueError, match="cannot move backwards"):
        clock.set_time(past)


def test_sim_clock_requires_utc() -> None:
    clock = SimClock()
    naive = datetime(2026, 10, 26, 9, 15, 0)
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        clock.set_time(naive)


def test_sim_clock_default_epoch() -> None:
    clock = SimClock()
    assert clock.now() == datetime(1970, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert clock.monotonic_ns() == 0
