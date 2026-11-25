"""Background watcher detecting OS/process suspension via wall vs monotonic clock drift."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SuspendEvent:
    """Recorded process suspension interval."""

    seconds: float
    wall_start: str
    wall_end: str


class SuspendWatcher:
    """Compares wall-clock and monotonic time to detect OS/process suspension."""

    def __init__(
        self,
        wall_fn: Callable[[], datetime],
        mono_fn: Callable[[], float],
        threshold_seconds: float,
        on_suspend: Callable[[SuspendEvent], None] | None = None,
    ) -> None:
        self._wall_fn = wall_fn
        self._mono_fn = mono_fn
        self.threshold_seconds = float(threshold_seconds)
        self._on_suspend = on_suspend
        self._last_wall: datetime = wall_fn()
        self._last_mono: float = mono_fn()
        self.suspended_events: int = 0
        self.suspended_seconds: float = 0.0

    def check_once(self) -> SuspendEvent | None:
        """Sample wall and monotonic clocks and emit a SuspendEvent on large jumps."""
        wall_now = self._wall_fn()
        mono_now = self._mono_fn()
        wall_delta = (wall_now - self._last_wall).total_seconds()
        mono_delta = mono_now - self._last_mono
        jump = wall_delta - mono_delta
        prev_wall = self._last_wall
        self._last_wall = wall_now
        self._last_mono = mono_now

        if jump > self.threshold_seconds:
            ev = SuspendEvent(
                seconds=round(jump, 3),
                wall_start=prev_wall.isoformat(),
                wall_end=wall_now.isoformat(),
            )
            self.suspended_events += 1
            self.suspended_seconds += jump
            if self._on_suspend is not None:
                self._on_suspend(ev)
            return ev
        return None

    async def run_loop(
        self,
        interval_seconds: float = 1.0,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Periodically run check_once until cancelled or stop_event is set."""
        while True:
            if stop_event is not None:
                if stop_event.is_set():
                    break
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
                    break
                except TimeoutError:
                    pass
            else:
                await asyncio.sleep(interval_seconds)
            self.check_once()
