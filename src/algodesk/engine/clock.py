"""Wall clock and simulation clock implementations.

Provides WallClock (reading system UTC time) and SimClock (driven deterministically
by simulation/replay events, advancing only through set_time, never backwards).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from algodesk.api.market import _validate_utc_ts


class WallClock:
    """Injected clock reading the real system wall-clock time in UTC."""

    __slots__ = ()

    def now(self) -> datetime:
        """Return the current timezone-aware UTC datetime."""
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        """Return the system monotonic clock in nanoseconds."""
        return time.monotonic_ns()


class SimClock:
    """Simulated clock advanced explicitly by replay or simulation events.

    Cannot move backwards. Monotonic nanoseconds advance proportionally with time.
    """

    __slots__ = ("_current", "_monotonic_ns")

    def __init__(self, initial_time: datetime | None = None) -> None:
        if initial_time is not None:
            _validate_utc_ts(initial_time, "initial_time")
            self._current: datetime = initial_time
        else:
            self._current = datetime(1970, 1, 1, 0, 0, 0, tzinfo=UTC)
        self._monotonic_ns: int = 0

    def set_time(self, ts: datetime) -> None:
        """Advance simulated time to ts.

        Raises ValueError if ts is backwards in time or not UTC.
        """
        _validate_utc_ts(ts, "ts")
        if ts < self._current:
            msg = (
                f"SimClock cannot move backwards: attempted {ts.isoformat()} "
                f"while current time is {self._current.isoformat()}"
            )
            raise ValueError(msg)
        delta_ns = int((ts - self._current).total_seconds() * 1_000_000_000)
        self._monotonic_ns += delta_ns
        self._current = ts

    def now(self) -> datetime:
        """Return current simulated UTC datetime."""
        return self._current

    def monotonic_ns(self) -> int:
        """Return simulated monotonic nanoseconds."""
        return self._monotonic_ns
