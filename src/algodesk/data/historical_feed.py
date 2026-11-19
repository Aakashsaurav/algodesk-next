"""HistoricalFeed: feeds cached historical bars and synthetic quotes for backtesting."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from algodesk.api.market import Bar, InstrumentId, Quote
from algodesk.data.bar_synth import BarQuoteSynth
from algodesk.data.readiness import resolve_backtest_series
from algodesk.data.service import DataService


class HistoricalFeed:
    """Historical market data feed reading purely from local cache without downloading."""

    def __init__(
        self,
        data_service: DataService,
        instruments: Sequence[InstrumentId],
        start: date | datetime,
        end: date | datetime,
        timeframe: str = "1d",
        source: str = "upstox",
        warmup_bars_count: int = 50,
        calendar: Any = None,
        manifest: Any = None,
        synth: BarQuoteSynth | None = None,
        adjust: str = "splits",
    ) -> None:
        self.data_service = data_service
        self.instruments = list(instruments)
        self.timeframe = timeframe
        self.source = source
        self.warmup_bars_count = max(0, warmup_bars_count)
        self.calendar = calendar
        self.manifest = manifest
        self.synth = synth or BarQuoteSynth()
        self.adjust = adjust

        # Normalize start/end datetimes in UTC
        if isinstance(start, date) and not isinstance(start, datetime):
            self.start_date = start
            self.start_dt = datetime.combine(start, time(0, 0), tzinfo=UTC)
        elif isinstance(start, str):
            self.start_date = date.fromisoformat(start)
            self.start_dt = datetime.combine(self.start_date, time(0, 0), tzinfo=UTC)
        else:
            self.start_dt = start.astimezone(UTC)
            self.start_date = self.start_dt.date()

        if isinstance(end, date) and not isinstance(end, datetime):
            self.end_date = end
            self.end_dt = datetime.combine(end, time(23, 59, 59, 999999), tzinfo=UTC)
        elif isinstance(end, str):
            self.end_date = date.fromisoformat(end)
            self.end_dt = datetime.combine(self.end_date, time(23, 59, 59, 999999), tzinfo=UTC)
        else:
            self.end_dt = end.astimezone(UTC)
            self.end_date = self.end_dt.date()

        self._warmup_bars: list[Bar] = []
        self._bars: list[Bar] = []
        self._loaded = False

    @property
    def warmup_bars(self) -> list[Bar]:
        """Warm-up bars prior to backtest start."""
        if not self._loaded:
            self._load()
        return self._warmup_bars

    @property
    def bars(self) -> list[Bar]:
        """Active window bars for the backtest."""
        if not self._loaded:
            self._load()
        return self._bars

    def _load(self) -> None:
        if self._loaded:
            return

        expected_sessions: set[date] | None = None
        if self.calendar is not None:
            try:
                from algodesk.data.coverage import is_trading_session

                curr = self.start_date
                expected_sessions = set()
                while curr <= self.end_date:
                    if is_trading_session(curr, calendar=self.calendar, manifest=self.manifest):
                        expected_sessions.add(curr)
                    curr += timedelta(days=1)
            except Exception:
                expected_sessions = None

        all_active_bars: list[Bar] = []
        all_warmup_bars: list[Bar] = []

        # Read in chunks or window per instrument with cache_only=True
        man = self.manifest or getattr(getattr(self.data_service, "store", None), "manifest", None)
        store = getattr(self.data_service, "store", None)
        for inst in self.instruments:
            series_key = f"{inst}:{self.timeframe}" if ":" not in str(inst)[7:] else str(inst)
            resolved = resolve_backtest_series(
                inst,
                timeframe=self.timeframe,
                source=self.source,
                adjust=self.adjust,
                manifest=man,
                store=store,
            )
            # 1. Warm-up read before start
            if self.warmup_bars_count > 0:
                # Estimate pre-start window: for 1d, 2.5x calendar days gives enough bars
                mult = 3 if self.timeframe == "1d" else 1
                w_days = max(10, self.warmup_bars_count * mult)
                w_start_dt = self.start_dt - timedelta(days=w_days)
                try:
                    w_slice = self.data_service.get(
                        series_key,
                        start=w_start_dt,
                        end=self.start_dt,
                        source=resolved.source,
                        download=resolved.adjustment,
                        cache_only=True,
                        adjust=resolved.adjust,
                    )
                    w_bars = [b for b in w_slice.bars if b.end_ts <= self.start_dt]
                    # Take last warmup_bars_count bars
                    all_warmup_bars.extend(w_bars[-self.warmup_bars_count :])
                except (KeyError, FileNotFoundError):
                    pass

            # 2. Active backtest window bars
            # Do NOT catch DataError so corporate action conflicts / manual_required raise loud
            act_slice = self.data_service.get(
                series_key,
                start=self.start_dt,
                end=self.end_dt,
                source=resolved.source,
                download=resolved.adjustment,
                cache_only=True,
                adjust=resolved.adjust,
            )
            for b in act_slice.bars:
                if b.start_ts > self.end_dt:
                    continue
                if b.end_ts <= self.start_dt:
                    continue
                if expected_sessions is not None and b.start_ts.date() not in expected_sessions:
                    continue
                all_active_bars.append(b)

        # Sort warm-up bars by (start_ts, instrument)
        all_warmup_bars.sort(key=lambda b: (b.start_ts, str(b.instrument)))
        self._warmup_bars = all_warmup_bars

        # Merge active bars by timestamp, breaking ties by instrument id
        all_active_bars.sort(key=lambda b: (b.start_ts, str(b.instrument)))
        self._bars = all_active_bars
        self._loaded = True

    def iter_with_seq(self) -> Iterator[tuple[Quote | Bar, int]]:
        """Yield synthetic quotes and bars sequentially in strictly monotonic time.

        Same-timestamp ordering:
        1. Closing quote of bar t (priority 0)
        2. BarClosed(t) (priority 1)
        3. Strategy and orders execute
        4. Opening quote of bar t+1
        Ties are broken deterministically by instrument ID.
        """
        if not self._loaded:
            self._load()

        # Build list of (timestamp, priority, instrument_str, event)
        all_events: list[tuple[datetime, int, str, Quote | Bar]] = []
        for bar in self._bars:
            if self.synth is not None:
                q_open, q_1, q_2, q_close = self.synth.synthesize(bar)
                inst_str = str(bar.instrument)
                all_events.append((q_open.recv_ts, 0, inst_str, q_open))
                all_events.append((q_1.recv_ts, 0, inst_str, q_1))
                all_events.append((q_2.recv_ts, 0, inst_str, q_2))
                all_events.append((q_close.recv_ts, 0, inst_str, q_close))
                all_events.append((bar.end_ts, 1, inst_str, bar))
            else:
                all_events.append((bar.end_ts, 1, str(bar.instrument), bar))

        # Sort by (timestamp, priority, instrument)
        all_events.sort(key=lambda item: (item[0], item[1], item[2]))

        seq = 0
        for _, _, _, ev in all_events:
            seq += 1
            yield ev, seq
