"""Bar builder aggregating ticks into completed candlestick bars.

Aligns bar periods to exchange time anchored at tradable session open.
Handles volume differences, boundary timers, late ticks, and flat bar synthesis.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from algodesk.api.market import Bar, InstrumentId, Tick
from algodesk.engine.bus import EventBus
from algodesk.engine.events import BarClosed, SystemEvent
from algodesk.engine.scheduler import Scheduler
from algodesk.engine.sessions import SessionManager


def parse_timeframe_seconds(tf: str) -> int:
    """Convert timeframe string e.g. '1m', '5m', '1h', '1d' to seconds."""
    s = tf.strip().lower()
    if s.endswith("s"):
        return int(s[:-1])
    if s.endswith("m"):
        return int(s[:-1]) * 60
    if s.endswith("h"):
        return int(s[:-1]) * 3600
    if s.endswith("d"):
        return int(s[:-1]) * 86400
    raise ValueError(f"Unsupported timeframe format: {tf!r}")


@dataclass(slots=True)
class _ActiveBar:
    instrument: InstrumentId
    timeframe: str
    start_ts: datetime
    end_ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    ticks_count: int = 0


class BarBuilder:
    """Builds completed OHLCV bars from incoming market ticks."""

    __slots__ = (
        "_active_bars",
        "_bar_close_grace_ms",
        "_bus",
        "_dropped_outside_count",
        "_fill_empty_bars",
        "_last_close_price",
        "_last_closed_end",
        "_last_cumulative_vol",
        "_late_tick_count",
        "_on_bar_closed",
        "_on_system_event",
        "_scheduler",
        "_session_manager",
        "_tf_seconds",
        "_timeframes",
    )

    def __init__(
        self,
        timeframes: Sequence[str] = ("1m", "5m"),
        session_manager: SessionManager | None = None,
        bus: EventBus | None = None,
        scheduler: Scheduler | None = None,
        bar_close_grace_ms: int = 500,
        fill_empty_bars: bool = True,
        on_bar_closed: Callable[[Bar], None] | None = None,
        on_system_event: Callable[[SystemEvent], None] | None = None,
    ) -> None:
        self._timeframes = list(timeframes)
        self._tf_seconds = {tf: parse_timeframe_seconds(tf) for tf in self._timeframes}
        self._session_manager = session_manager
        self._bus = bus
        self._scheduler = scheduler
        self._bar_close_grace_ms = bar_close_grace_ms
        self._fill_empty_bars = fill_empty_bars
        self._on_bar_closed = on_bar_closed
        self._on_system_event = on_system_event

        self._active_bars: dict[tuple[InstrumentId, str], _ActiveBar] = {}
        self._last_cumulative_vol: dict[InstrumentId, float] = {}
        self._last_close_price: dict[tuple[InstrumentId, str], float] = {}
        self._last_closed_end: dict[tuple[InstrumentId, str], datetime] = {}
        self._late_tick_count: int = 0
        self._dropped_outside_count: int = 0

    @property
    def late_tick_count(self) -> int:
        return self._late_tick_count

    @property
    def dropped_outside_count(self) -> int:
        return self._dropped_outside_count

    def on_tick(self, tick: Tick) -> None:
        """Process tick across all configured timeframes."""
        session_start = self._resolve_session_start(tick)
        if session_start is None:
            self._dropped_outside_count += 1
            return

        vol_delta = self._compute_vol_delta(tick)
        for tf in self._timeframes:
            self._process_tick_tf(tick, tf, session_start, vol_delta)

    def _resolve_session_start(self, tick: Tick) -> datetime | None:
        """Resolve tradable session open time, or None if outside tradable session."""
        if self._session_manager is not None:
            session = self._session_manager.session_for(tick.instrument, tick.exch_ts)
            if session is None or not session.tradable:
                return None
            return session.start
        return tick.exch_ts.replace(hour=0, minute=0, second=0, microsecond=0)

    def _compute_vol_delta(self, tick: Tick) -> int:
        """Calculate volume difference of cumulative feed volume with reset handling."""
        prev = self._last_cumulative_vol.get(tick.instrument)
        if prev is None or tick.volume < prev:
            delta = int(tick.volume)
        else:
            delta = int(tick.volume - prev)
        self._last_cumulative_vol[tick.instrument] = tick.volume
        return max(0, delta)

    def _calculate_period(
        self,
        ts: datetime,
        session_start: datetime,
        tf_sec: int,
    ) -> tuple[datetime, datetime]:
        """Compute period boundary anchored at session_start."""
        diff_s = int((ts - session_start).total_seconds())
        period_idx = diff_s // tf_sec
        p_start = session_start + timedelta(seconds=period_idx * tf_sec)
        p_end = p_start + timedelta(seconds=tf_sec)
        return p_start, p_end

    def _process_tick_tf(
        self,
        tick: Tick,
        tf: str,
        session_start: datetime,
        vol_delta: int,
    ) -> None:
        key = (tick.instrument, tf)
        tf_sec = self._tf_seconds[tf]
        p_start, p_end = self._calculate_period(tick.exch_ts, session_start, tf_sec)

        active = self._active_bars.get(key)
        if active is not None:
            if tick.exch_ts < active.start_ts:
                self._handle_late_tick(tick, active)
                return
            if tick.exch_ts >= active.end_ts:
                self._close_active_bar(key, p_start)
                active = None

        if active is None:
            last_end = self._last_closed_end.get(key)
            if last_end is not None and last_end < p_start and self._fill_empty_bars:
                self._fill_empty(tick.instrument, tf, last_end, p_start, tf_sec)
            active = _ActiveBar(
                instrument=tick.instrument,
                timeframe=tf,
                start_ts=p_start,
                end_ts=p_end,
                open=tick.ltp,
                high=tick.ltp,
                low=tick.ltp,
                close=tick.ltp,
                volume=vol_delta,
                ticks_count=1,
            )
            self._active_bars[key] = active
            self._schedule_boundary_timer(key, p_end)
        else:
            active.high = max(active.high, tick.ltp)
            active.low = min(active.low, tick.ltp)
            active.close = tick.ltp
            active.volume += vol_delta
            active.ticks_count += 1

    def _handle_late_tick(self, tick: Tick, active: _ActiveBar) -> None:
        self._late_tick_count += 1
        msg = (
            f"Late tick for {tick.instrument} at {tick.exch_ts.isoformat()} "
            f"arrived after bar {active.start_ts.isoformat()}"
        )
        evt = SystemEvent(
            kind="feed.late_tick",
            message=msg,
            ts=tick.recv_ts,
            details={"instrument": str(tick.instrument), "exch_ts": tick.exch_ts.isoformat()},
        )
        if self._bus is not None:
            self._bus.publish(evt)
        if self._on_system_event is not None:
            self._on_system_event(evt)

    def _schedule_boundary_timer(
        self,
        key: tuple[InstrumentId, str],
        period_end: datetime,
    ) -> None:
        if self._scheduler is None:
            return
        inst, tf = key
        timer_name = f"bar_close.{inst}.{tf}.{period_end.isoformat()}"
        due = period_end + timedelta(milliseconds=self._bar_close_grace_ms)

        def _cb() -> None:
            self._on_boundary_timer(key, period_end)

        self._scheduler.schedule_at(timer_name, due, _cb)

    def _on_boundary_timer(
        self,
        key: tuple[InstrumentId, str],
        period_end: datetime,
    ) -> None:
        active = self._active_bars.get(key)
        if active is not None and active.end_ts <= period_end:
            self._close_active_bar(key, period_end)

    def _close_active_bar(
        self,
        key: tuple[InstrumentId, str],
        next_period_start: datetime,
    ) -> None:
        active = self._active_bars.pop(key, None)
        if active is None:
            return
        bar = Bar(
            instrument=active.instrument,
            timeframe=active.timeframe,
            start_ts=active.start_ts,
            end_ts=active.end_ts,
            open=active.open,
            high=active.high,
            low=active.low,
            close=active.close,
            volume=active.volume,
            complete=True,
            synthetic=False,
        )
        self._last_close_price[key] = active.close
        self._last_closed_end[key] = active.end_ts

        if self._bus is not None:
            self._bus.publish(BarClosed(bar=bar))
        if self._on_bar_closed is not None:
            self._on_bar_closed(bar)

    def _fill_empty(
        self,
        instrument: InstrumentId,
        tf: str,
        from_ts: datetime,
        to_ts: datetime,
        tf_sec: int,
    ) -> None:
        key = (instrument, tf)
        last_px = self._last_close_price.get(key, 0.0)
        curr = from_ts
        step = timedelta(seconds=tf_sec)
        while curr < to_ts:
            nxt = curr + step
            flat = Bar(
                instrument=instrument,
                timeframe=tf,
                start_ts=curr,
                end_ts=nxt,
                open=last_px,
                high=last_px,
                low=last_px,
                close=last_px,
                volume=0,
                complete=True,
                synthetic=True,
            )
            self._last_close_price[key] = last_px
            self._last_closed_end[key] = nxt
            if self._bus is not None:
                self._bus.publish(BarClosed(bar=flat))
            if self._on_bar_closed is not None:
                self._on_bar_closed(flat)
            curr = nxt
