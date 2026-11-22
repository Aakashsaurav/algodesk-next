"""TickBarFeed: rebuilds 1m bars from recorded ticks and timer closes."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from algodesk.api.market import Bar, InstrumentId, Quote, Tick
from algodesk.data.bar_synth import BarQuoteSynth
from algodesk.data.replay import ReplayFeed, load_warmup_bars_parquet

# Note: BarBuilder imported lazily in _reconstruct to maintain architecture layer boundaries

logger = logging.getLogger("algodesk.data.tick_bar_feed")


class TickBarFeed:
    """Rebuilds 1m bars of a recorded paper session from ticks and timer closes.

    Honours recorded bar.timer_close events at their exact recorded tick sequence
    positions, yielding bars and synthetic quotes conforming to HistoricalFeed.
    """

    def __init__(
        self,
        session_dir: Path | str,
        data_dir: Path | str | None = None,
        timeframe: str = "1m",
        instruments: Sequence[InstrumentId | str] | None = None,
        calendar: Any = None,
        calendars: Mapping[str, Any] | None = None,
        session_manager: Any = None,
        synth: BarQuoteSynth | None = None,
        tick_window: tuple[datetime, datetime] | None = None,
    ) -> None:
        self.session_dir = Path(session_dir)
        if data_dir is not None:
            self.data_dir = Path(data_dir)
        else:
            # Default to ../../data relative to results/<session_id>
            self.data_dir = self.session_dir.parent.parent / "data"

        self.timeframe = timeframe
        self.instruments = [str(i) for i in instruments] if instruments else None
        self.calendar = calendar
        self.calendars = calendars
        self._session_manager = session_manager
        self.synth = synth or BarQuoteSynth(calendar=calendar)
        self.tick_window = tick_window

        self._warmup_bars: list[Bar] = []
        self._bars: list[Bar] = []
        self._loaded: bool = False

    @property
    def warmup_bars(self) -> list[Bar]:
        """Warm-up bars prior to session start."""
        if not self._loaded:
            self._load()
        return self._warmup_bars

    @property
    def bars(self) -> list[Bar]:
        """Rebuilt bars from recorded ticks."""
        if not self._loaded:
            self._load()
        return self._bars

    def _load(self) -> None:
        if self._loaded:
            return

        # 1. Load warmup bars if saved
        wb_path = self.session_dir / "warmup_bars.parquet"
        if wb_path.is_file():
            try:
                self._warmup_bars = load_warmup_bars_parquet(wb_path)
            except Exception as exc:
                logger.debug("Failed loading warmup_bars.parquet: %s", exc)
                self._warmup_bars = []
        else:
            self._warmup_bars = []

        # 2. Parse recorded bar.timer_close events from events.jsonl
        timer_closes_by_seq: dict[int, list[dict[str, Any]]] = {}
        events_jsonl = self.session_dir / "events.jsonl"
        if events_jsonl.is_file():
            for line in events_jsonl.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                    k = e.get("kind")
                    p = e.get("payload_json", e.get("payload", {}))
                    if isinstance(p, str):
                        p = json.loads(p)
                    seq = p.get("seq")
                    if k == "bar.timer_close" and seq is not None:
                        timer_closes_by_seq.setdefault(int(seq), []).append(p)
                except Exception:
                    continue

        # 3. Locate tick files for session
        session_id = self.session_dir.name
        files: list[Path] = []
        seen: set[str] = set()

        # Check session_dir itself, data/ticks/<session_id>, data/ticks
        search_dirs = [
            self.session_dir,
            self.data_dir / "ticks" / session_id,
            self.data_dir / "ticks",
        ]
        for s_dir in search_dirs:
            if s_dir.is_dir():
                for p in sorted(s_dir.glob("**/*.parquet")):
                    if p.name == "warmup_bars.parquet":
                        continue
                    if str(p) not in seen:
                        seen.add(str(p))
                        files.append(p)

        # 4. Resolve tick window from manifest if not provided
        w_start: datetime | None = None
        w_end: datetime | None = None
        if self.tick_window:
            w_start, w_end = self.tick_window
        else:
            m_path = self.session_dir / "manifest.json"
            if m_path.is_file():
                try:
                    m_data = json.loads(m_path.read_text(encoding="utf-8"))
                    if m_data.get("first_tick_utc") and m_data.get("last_tick_utc"):
                        w_start = datetime.fromisoformat(m_data["first_tick_utc"])
                        w_end = datetime.fromisoformat(m_data["last_tick_utc"])
                    elif m_data.get("first_tick_recv_utc") and m_data.get("last_tick_recv_utc"):
                        w_start = datetime.fromisoformat(m_data["first_tick_recv_utc"])
                        w_end = datetime.fromisoformat(m_data["last_tick_recv_utc"])
                except Exception:
                    pass

        # 5. Resolve session manager
        session_mgr = self._session_manager

        # 6. Stream ticks through BarBuilder honoring timer closes
        closed_bars: list[Bar] = []

        def _on_closed(b: Bar) -> None:
            if self.instruments is None or str(b.instrument) in self.instruments:
                closed_bars.append(b)

        from importlib import import_module

        bar_builder_cls = import_module("algodesk.engine.bars").BarBuilder
        bar_builder = bar_builder_cls(
            timeframes=[self.timeframe],
            session_manager=session_mgr,
            fill_empty_bars=False,
            on_bar_closed=_on_closed,
        )

        if files:
            feed = ReplayFeed(
                files=files,
                window_start=w_start,
                window_end=w_end,
                session_id=session_id,
            )

            for ev, seq in feed.iter_with_seq():
                if not isinstance(ev, (Tick, Quote)):
                    continue
                # Strictly filter ticks to recorded window bounds if known
                if w_start is not None and ev.exch_ts < w_start:
                    continue
                if w_end is not None and ev.exch_ts > w_end:
                    continue

                # Feed tick first
                if isinstance(ev, Tick):
                    bar_builder.on_tick(ev)

                if seq is not None:
                    # Dispatch any timer closes scheduled at or before this sequence
                    to_dispatch = [k for k in sorted(timer_closes_by_seq.keys()) if k <= seq]
                    for k in to_dispatch:
                        for tc in timer_closes_by_seq.pop(k, []):
                            inst_str = tc.get("instrument")
                            tf = tc.get("timeframe", self.timeframe)
                            end_ts_val = tc.get("end_ts")
                            if inst_str and end_ts_val:
                                end_dt = datetime.fromisoformat(end_ts_val)
                                bar_builder.close_bar_timer(inst_str, tf, end_dt)

        # Flush any remaining timer closes that occurred after last tick
        for seq_key in sorted(timer_closes_by_seq.keys()):
            for tc in timer_closes_by_seq[seq_key]:
                inst_str = tc.get("instrument")
                tf = tc.get("timeframe", self.timeframe)
                end_ts_val = tc.get("end_ts")
                if inst_str and end_ts_val:
                    end_dt = datetime.fromisoformat(end_ts_val)
                    bar_builder.close_bar_timer(inst_str, tf, end_dt)

        # Note: Do not forcibly close incomplete unclosed active bars at session end,
        # preserving exact equivalence with paper mode where incomplete bars are not emitted.

        # Sort bars by (start_ts, instrument)
        closed_bars.sort(key=lambda b: (b.start_ts, str(b.instrument)))
        self._bars = closed_bars
        self._loaded = True

    def iter_with_seq(self) -> Iterator[tuple[Quote | Bar, int]]:
        """Yield synthetic quotes and bars sequentially in strictly monotonic time."""
        if not self._loaded:
            self._load()

        all_events: list[tuple[datetime, int, str, Quote | Bar]] = []
        for bar in self._bars:
            if self.synth is not None:
                q_open, q_1, q_2, q_close = self.synth.synthesize(bar)
                inst_str = str(bar.instrument)
                all_events.append((q_open.recv_ts, 0, inst_str, q_open))
                all_events.append((q_1.recv_ts, 0, inst_str, q_1))
                all_events.append((q_2.recv_ts, 0, inst_str, q_2))
                all_events.append((q_close.recv_ts, 0, inst_str, q_close))
                feed_bar = Bar(
                    instrument=bar.instrument,
                    timeframe=bar.timeframe,
                    start_ts=q_open.recv_ts,
                    end_ts=q_close.recv_ts,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    complete=bar.complete,
                    synthetic=bar.synthetic,
                )
                all_events.append((q_close.recv_ts, 1, inst_str, feed_bar))
            else:
                all_events.append((bar.end_ts, 1, str(bar.instrument), bar))

        all_events.sort(key=lambda item: (item[0], item[1], item[2]))

        seq = 0
        for _, _, _, ev in all_events:
            seq += 1
            yield ev, seq
