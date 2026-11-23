"""BarQuoteSynth: synthesizes order book quotes from OHLCV bars for backtesting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from algodesk.api.market import Bar, Level, Quote

IST = ZoneInfo("Asia/Kolkata")


class BarQuoteSynth:
    """Synthesizes quotes from completed bars with participation depth and delay rules."""

    def __init__(
        self,
        participation_pct: float = 10.0,
        price_tick: float = 0.01,
        calendar: Any = None,
    ) -> None:
        self.participation_pct = max(0.01, min(100.0, float(participation_pct)))
        self.price_tick = Decimal(str(price_tick))
        self.calendar = calendar

    def _daily_timestamps(self, bar: Bar) -> list[datetime]:
        """Compute 4 timestamps for daily bars: session open (09:15 IST), 2 mid, session close."""
        ts = bar.start_ts if bar.start_ts.tzinfo else bar.start_ts.replace(tzinfo=UTC)
        d = ts.astimezone(IST).date()
        s_open: datetime | None = None
        s_close: datetime | None = None

        if self.calendar is not None:
            try:
                sessions = self.calendar.sessions(d, segment="eq")
                if sessions:
                    s_open = sessions[0].start
                    s_close = sessions[0].end
            except Exception:
                pass

        if s_open is None or s_close is None:
            if (bar.end_ts - bar.start_ts) >= timedelta(hours=12):
                s_open = datetime(d.year, d.month, d.day, 3, 45, 0, tzinfo=UTC)
                s_close = datetime(d.year, d.month, d.day, 10, 0, 0, tzinfo=UTC)
            else:
                s_open = bar.start_ts
                s_close = bar.end_ts

        duration = s_close - s_open
        if duration > timedelta(0):
            d_step = duration / 3
            return [s_open, s_open + d_step, s_open + d_step * 2, s_close]
        return [s_open, s_open, s_open, s_close]

    def _intraday_timestamps(self, bar: Bar) -> list[datetime]:
        """Compute 4 timestamps inside intraday bar period: open, 2 mid, close (end - 1ms)."""
        c = bar.end_ts - timedelta(milliseconds=1)
        if bar.timeframe == "1m":
            s = bar.start_ts
            return [s, s + timedelta(seconds=20), s + timedelta(seconds=40), c]
        duration = c - bar.start_ts
        if duration > timedelta(0):
            d_step = duration / 3
            return [bar.start_ts, bar.start_ts + d_step, bar.start_ts + d_step * 2, c]
        return [bar.start_ts, bar.start_ts, bar.start_ts, c]

    def synthesize(self, bar: Bar) -> tuple[Quote, Quote, Quote, Quote]:
        """Convert a single OHLCV bar into 4 quotes: O, then L/H or H/L, then C.

        For an up bar (close >= open): O, L, H, C.
        For a down bar (close < open): O, H, L, C.
        Depth equals backtest.participation_pct * bar.volume.
        Quotes are built through an unchecked constructor after the source bar was validated.
        """
        if bar.open <= 0 or bar.high < bar.low:
            msg = f"Invalid bar parameters: open={bar.open}, high={bar.high}, low={bar.low}"
            raise ValueError(msg)

        vol = max(1, int(bar.volume))
        depth = max(1, int(vol * self.participation_pct / 100.0))

        if bar.close >= bar.open:
            prices = (bar.open, bar.low, bar.high, bar.close)
        else:
            prices = (bar.open, bar.high, bar.low, bar.close)

        is_daily = (bar.timeframe == "1d") or ((bar.end_ts - bar.start_ts) >= timedelta(hours=20))
        timestamps = self._daily_timestamps(bar) if is_daily else self._intraday_timestamps(bar)

        inst = bar.instrument
        p0, p1, p2, p3 = (round(float(p), 4) for p in prices)
        t0, t1, t2, t3 = timestamps

        lvl0 = (Level.create_unchecked(price=p0, qty=depth),)
        lvl1 = (Level.create_unchecked(price=p1, qty=depth),)
        lvl2 = (Level.create_unchecked(price=p2, qty=depth),)
        lvl3 = (Level.create_unchecked(price=p3, qty=depth),)

        q0 = Quote.create_unchecked(
            instrument=inst, exch_ts=t0, recv_ts=t0, bids=lvl0, asks=lvl0, ltp=p0, volume=vol
        )
        q1 = Quote.create_unchecked(
            instrument=inst, exch_ts=t1, recv_ts=t1, bids=lvl1, asks=lvl1, ltp=p1, volume=vol
        )
        q2 = Quote.create_unchecked(
            instrument=inst, exch_ts=t2, recv_ts=t2, bids=lvl2, asks=lvl2, ltp=p2, volume=vol
        )
        q3 = Quote.create_unchecked(
            instrument=inst, exch_ts=t3, recv_ts=t3, bids=lvl3, asks=lvl3, ltp=p3, volume=vol
        )

        return (q0, q1, q2, q3)
