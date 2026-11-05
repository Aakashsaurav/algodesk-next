"""Deterministic replay feed over recorded Parquet files driving a SimClock.

Performs a k-way merge across files in (recv_ts, exch_ts, instrument) order
using streaming Parquet batches to guarantee bounded memory.
Also provides warm-up bar persistence and session tick window filtering.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

from algodesk.api.market import Bar, InstrumentId, MarketEvent, Quote, Tick
from algodesk.data.schema import row_to_bar, row_to_market_event

_DEFAULT_EXCH_TZ = ZoneInfo("Asia/Kolkata")

WARMUP_BAR_FIELDS: list[pa.Field] = [
    pa.field("instrument", pa.string(), nullable=False),
    pa.field("timeframe", pa.string(), nullable=False),
    pa.field("start_ts", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("end_ts", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("exch_ts", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("exch_time", pa.string(), nullable=False),
    pa.field("open", pa.float64(), nullable=False),
    pa.field("high", pa.float64(), nullable=False),
    pa.field("low", pa.float64(), nullable=False),
    pa.field("close", pa.float64(), nullable=False),
    pa.field("volume", pa.int64(), nullable=False),
    pa.field("complete", pa.bool_(), nullable=False),
    pa.field("synthetic", pa.bool_(), nullable=False),
]
WARMUP_BAR_SCHEMA = pa.schema(WARMUP_BAR_FIELDS)


def save_warmup_bars_parquet(
    path: Path | str,
    bars: Sequence[Bar],
    exchange_tz: tzinfo | None = None,
) -> None:
    """Save warm-up bars across multiple instruments and timeframes to Parquet."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not bars:
        table = pa.Table.from_batches([], schema=WARMUP_BAR_SCHEMA)
    else:
        sorted_bars = sorted(bars, key=lambda b: (str(b.instrument), b.start_ts))
        cols: dict[str, list[Any]] = {f.name: [] for f in WARMUP_BAR_SCHEMA}
        tz = exchange_tz or _DEFAULT_EXCH_TZ
        for b in sorted_bars:
            exch_dt = b.start_ts.astimezone(tz)
            cols["instrument"].append(str(b.instrument))
            cols["timeframe"].append(str(getattr(b, "timeframe", "1m")))
            cols["start_ts"].append(b.start_ts)
            cols["end_ts"].append(b.end_ts)
            cols["exch_ts"].append(b.start_ts)
            cols["exch_time"].append(exch_dt.isoformat())
            cols["open"].append(float(b.open))
            cols["high"].append(float(b.high))
            cols["low"].append(float(b.low))
            cols["close"].append(float(b.close))
            cols["volume"].append(int(b.volume))
            cols["complete"].append(bool(getattr(b, "complete", True)))
            cols["synthetic"].append(bool(getattr(b, "synthetic", False)))
        table = pa.Table.from_pydict(cols, schema=WARMUP_BAR_SCHEMA)
    pq.write_table(table, p, compression="zstd")


def load_warmup_bars_parquet(path: Path | str) -> list[Bar]:
    """Load warm-up bars from Parquet preserving instruments and timeframes."""
    p = Path(path)
    if not p.is_file():
        return []
    table = pq.read_table(p)
    pydict = table.to_pydict()
    if not pydict or "start_ts" not in pydict:
        return []
    n = len(pydict["start_ts"])
    bars: list[Bar] = []
    for i in range(n):
        inst = InstrumentId(pydict["instrument"][i])
        tf = str(pydict["timeframe"][i])
        row = {k: pydict[k][i] for k in pydict}
        bars.append(row_to_bar(inst, tf, row))
    return bars


def _derive_instrument_from_file_path(path: Path) -> InstrumentId:
    """Extract canonical instrument id from partitioned path."""
    exch, seg, sym = "", "", ""
    for part in path.parts:
        if part.startswith("exchange="):
            exch = part.split("=")[1]
        elif part.startswith("segment="):
            seg = part.split("=")[1]
        elif part.startswith("symbol="):
            sym = part.split("=")[1]
    if exch and seg and sym:
        return InstrumentId(f"{exch}:{seg}:{sym}".upper())
    return InstrumentId("NSE:EQ:UNKNOWN")


@dataclass(order=True)
class _ReplayItem:
    recv_ts: datetime
    exch_ts: datetime
    instrument_str: str
    seq: int
    event: Tick | Quote = field(compare=False)
    stream_idx: int = field(compare=False)


class _StreamReader:
    """Streams MarketEvents from a single Parquet file with window filtering."""

    __slots__ = (
        "_batch_iter",
        "_current_batch",
        "_idx",
        "_instrument",
        "_path",
        "_window_end",
        "_window_start",
    )

    def __init__(
        self,
        path: Path,
        batch_size: int = 1000,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> None:
        self._path = path
        self._instrument = _derive_instrument_from_file_path(path)
        self._window_start = window_start
        self._window_end = window_end
        parquet_file = pq.ParquetFile(path)
        self._batch_iter = parquet_file.iter_batches(batch_size=batch_size)
        self._current_batch: list[dict[str, Any]] = []
        self._idx = 0
        self._fetch_next_batch()

    def _fetch_next_batch(self) -> None:
        try:
            record_batch = next(self._batch_iter)
            self._current_batch = record_batch.to_pylist()
            self._idx = 0
        except StopIteration:
            self._current_batch = []
            self._idx = 0

    def next_event(self) -> Tick | Quote | None:
        while True:
            if self._idx >= len(self._current_batch):
                self._fetch_next_batch()
            if not self._current_batch:
                return None
            row = self._current_batch[self._idx]
            self._idx += 1
            ev = row_to_market_event(self._instrument, row)
            if ev is None:
                continue
            if self._window_start is not None and ev.recv_ts < self._window_start:
                continue
            if self._window_end is not None and ev.recv_ts > self._window_end:
                return None
            return ev


class ReplayFeed:
    """K-way merge replay feed driving a SimClock with optional tick window."""

    __slots__ = ("_batch_size", "_clock", "_files", "_window_end", "_window_start")

    def __init__(
        self,
        files: Sequence[Path | str],
        clock: Any = None,
        batch_size: int = 1000,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> None:
        self._files = [Path(f) for f in files if Path(f).exists()]
        self._clock = clock
        self._batch_size = batch_size
        self._window_start = window_start
        self._window_end = window_end

    def __iter__(self) -> Iterator[MarketEvent]:
        heap: list[_ReplayItem] = []
        streams: list[_StreamReader] = []
        seq = 0

        for idx, f in enumerate(self._files):
            reader = _StreamReader(
                f,
                batch_size=self._batch_size,
                window_start=self._window_start,
                window_end=self._window_end,
            )
            streams.append(reader)
            ev = reader.next_event()
            if ev is not None:
                seq += 1
                heapq.heappush(
                    heap,
                    _ReplayItem(
                        recv_ts=ev.recv_ts,
                        exch_ts=ev.exch_ts,
                        instrument_str=str(ev.instrument),
                        seq=seq,
                        event=ev,
                        stream_idx=idx,
                    ),
                )

        if self._clock is not None and hasattr(self._clock, "set_time"):
            if self._window_start is not None:
                if self._clock.now() < self._window_start:
                    self._clock.set_time(self._window_start)
            elif heap and self._clock.now() < heap[0].recv_ts:
                self._clock.set_time(heap[0].recv_ts)

        while heap:
            item = heapq.heappop(heap)
            if self._clock is not None and hasattr(self._clock, "set_time"):
                if self._clock.now() < item.event.recv_ts:
                    self._clock.set_time(item.event.recv_ts)

            yield item.event

            reader = streams[item.stream_idx]
            next_ev = reader.next_event()
            if next_ev is not None:
                seq += 1
                heapq.heappush(
                    heap,
                    _ReplayItem(
                        recv_ts=next_ev.recv_ts,
                        exch_ts=next_ev.exch_ts,
                        instrument_str=str(next_ev.instrument),
                        seq=seq,
                        event=next_ev,
                        stream_idx=item.stream_idx,
                    ),
                )
