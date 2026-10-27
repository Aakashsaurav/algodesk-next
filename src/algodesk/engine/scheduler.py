"""Deterministic timer scheduler driven by an injected Clock.

Supports:
1. Named one-shot and repeating timers.
2. Deterministic execution order strictly by (due_time, sequence_id).
3. Discrete advancement in simulation mode (advance_to).
4. Asyncio event loop runner in wall-clock mode.
"""

from __future__ import annotations

import asyncio
import heapq
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from algodesk.api.market import _validate_utc_ts
from algodesk.api.protocols import Clock


@dataclass(order=True)
class _TimerEntry:
    due: datetime
    sequence: int
    name: str = field(compare=False)
    callback: Callable[..., Any] = field(compare=False)
    interval_s: float | None = field(compare=False, default=None)
    cancelled: bool = field(compare=False, default=False)


def _invoke_callback(callback: Callable[..., Any], name: str) -> None:
    """Invoke callback passing timer name if accepted, else zero args."""
    try:
        callback(name)
    except TypeError:
        callback()


class Scheduler:
    """Timer scheduler ordered by due time and registration sequence."""

    __slots__ = ("_clock", "_heap", "_seq", "_timers")

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._heap: list[_TimerEntry] = []
        self._timers: dict[str, _TimerEntry] = {}
        self._seq: int = 0

    @property
    def clock(self) -> Clock:
        """The clock backing this scheduler."""
        return self._clock

    def schedule_at(
        self,
        name: str,
        due: datetime,
        callback: Callable[..., Any],
    ) -> None:
        """Schedule a one-shot timer at an explicit UTC datetime."""
        _validate_utc_ts(due, "due")
        if not name:
            raise ValueError("Timer name cannot be empty")
        self.cancel(name)
        self._seq += 1
        entry = _TimerEntry(
            due=due,
            sequence=self._seq,
            name=name,
            callback=callback,
        )
        self._timers[name] = entry
        heapq.heappush(self._heap, entry)

    def schedule_after(
        self,
        name: str,
        delay_seconds: float,
        callback: Callable[..., Any],
    ) -> None:
        """Schedule a one-shot timer after delay_seconds from now."""
        due = self._clock.now() + timedelta(seconds=delay_seconds)
        self.schedule_at(name, due, callback)

    def schedule_repeating(
        self,
        name: str,
        start: datetime,
        interval_seconds: float,
        callback: Callable[..., Any],
    ) -> None:
        """Schedule a repeating timer starting at start."""
        _validate_utc_ts(start, "start")
        if not name:
            raise ValueError("Timer name cannot be empty")
        if interval_seconds <= 0:
            raise ValueError(f"interval_seconds must be positive, got {interval_seconds}")
        self.cancel(name)
        self._seq += 1
        entry = _TimerEntry(
            due=start,
            sequence=self._seq,
            name=name,
            callback=callback,
            interval_s=interval_seconds,
        )
        self._timers[name] = entry
        heapq.heappush(self._heap, entry)

    def cancel(self, name: str) -> bool:
        """Cancel an existing timer by name. Returns True if cancelled."""
        entry = self._timers.pop(name, None)
        if entry is not None:
            entry.cancelled = True
            return True
        return False

    def has_timer(self, name: str) -> bool:
        """Return True if an active timer with name is scheduled."""
        return name in self._timers

    @property
    def pending_count(self) -> int:
        """Return the count of active pending timers."""
        return len(self._timers)

    def advance_to(self, ts: datetime) -> int:
        """Advance time to ts and fire all due timers in deterministic order."""
        _validate_utc_ts(ts, "ts")
        fired = 0
        set_time_fn = getattr(self._clock, "set_time", None)

        while self._heap and self._heap[0].due <= ts:
            entry = heapq.heappop(self._heap)
            if entry.cancelled:
                continue

            if set_time_fn is not None and self._clock.now() < entry.due:
                set_time_fn(entry.due)

            _invoke_callback(entry.callback, entry.name)
            fired += 1

            if entry.interval_s is not None and not entry.cancelled:
                self._seq += 1
                next_due = entry.due + timedelta(seconds=entry.interval_s)
                next_entry = _TimerEntry(
                    due=next_due,
                    sequence=self._seq,
                    name=entry.name,
                    callback=entry.callback,
                    interval_s=entry.interval_s,
                )
                self._timers[entry.name] = next_entry
                heapq.heappush(self._heap, next_entry)
            else:
                self._timers.pop(entry.name, None)

        if set_time_fn is not None and self._clock.now() < ts:
            set_time_fn(ts)

        return fired

    def fire_due(self) -> int:
        """Fire any timers currently due according to clock.now()."""
        return self.advance_to(self._clock.now())

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        """Asyncio loop for wall-clock execution."""
        poll_sleep_duration = 0.05
        while stop_event is None or not stop_event.is_set():
            now = self._clock.now()
            if self._heap:
                earliest = self._heap[0].due
                if earliest <= now:
                    self.fire_due()
                    continue
                wait_sec = min((earliest - now).total_seconds(), poll_sleep_duration)
                if wait_sec > 0:
                    await asyncio.sleep(wait_sec)
                else:
                    self.fire_due()
            else:
                await asyncio.sleep(poll_sleep_duration)
