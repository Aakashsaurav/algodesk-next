"""BarQuoteSynth: synthesizes order book quotes from OHLCV bars for backtesting."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from algodesk.api.market import Bar, Level, Quote


class BarQuoteSynth:
    """Synthesizes quotes from completed bars with participation depth and delay rules."""

    def __init__(
        self,
        participation_pct: float = 10.0,
        price_tick: float = 0.01,
    ) -> None:
        self.participation_pct = max(0.01, min(100.0, float(participation_pct)))
        self.price_tick = Decimal(str(price_tick))

    def synthesize(self, bar: Bar) -> tuple[Quote, Quote, Quote, Quote]:
        """Convert a single OHLCV bar into 4 quotes: O, then L/H or H/L, then C.

        For an up bar (close >= open): O, L, H, C.
        For a down bar (close < open): O, H, L, C.
        Depth equals backtest.participation_pct * bar.volume.
        """
        vol = max(1, int(bar.volume))
        depth = max(1, int(vol * self.participation_pct / 100.0))

        if bar.close >= bar.open:
            prices = [bar.open, bar.low, bar.high, bar.close]
        else:
            prices = [bar.open, bar.high, bar.low, bar.close]

        duration = bar.end_ts - bar.start_ts
        if duration > timedelta(0):
            d = duration / 4
            timestamps = [
                bar.start_ts,
                bar.start_ts + d,
                bar.start_ts + d * 2,
                bar.end_ts,
            ]
        else:
            timestamps = [bar.start_ts, bar.start_ts, bar.start_ts, bar.end_ts]

        quotes: list[Quote] = []
        for p, ts in zip(prices, timestamps, strict=True):
            p_flt = round(float(p), 4)
            q = Quote(
                instrument=bar.instrument,
                exch_ts=ts,
                recv_ts=ts,
                bids=(Level(price=p_flt, qty=depth),),
                asks=(Level(price=p_flt, qty=depth),),
                ltp=p_flt,
                volume=vol,
            )
            quotes.append(q)

        return (quotes[0], quotes[1], quotes[2], quotes[3])
