"""Deterministic replay feed over recorded Parquet files driving a SimClock.

Performs a k-way merge across files in (recv_ts, exch_ts, instrument) order
using streaming Parquet batches to guarantee bounded memory.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from algodesk.api.market import InstrumentId, MarketEvent, Quote, Tick
from algodesk.data.schema import row_to_market_event


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
    """Streams MarketEvents from a single Parquet file in bounded record batches."""

    __slots__ = ("_batch_iter", "_current_batch", "_idx", "_instrument", "_path")

    def __init__(self, path: Path, batch_size: int = 1000) -> None:
        self._path = path
        self._instrument = _derive_instrument_from_file_path(path)
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
        if self._idx >= len(self._current_batch):
            self._fetch_next_batch()
        if not self._current_batch:
            return None
        row = self._current_batch[self._idx]
        self._idx += 1
        return row_to_market_event(self._instrument, row)


class ReplayFeed:
    """K-way merge replay feed driving a SimClock."""

    __slots__ = ("_batch_size", "_clock", "_files")

    def __init__(
        self,
        files: Sequence[Path | str],
        clock: Any = None,
        batch_size: int = 1000,
    ) -> None:
        self._files = [Path(f) for f in files if Path(f).exists()]
        self._clock = clock
        self._batch_size = batch_size

    def __iter__(self) -> Iterator[MarketEvent]:
        heap: list[_ReplayItem] = []
        streams: list[_StreamReader] = []
        seq = 0

        for idx, f in enumerate(self._files):
            reader = _StreamReader(f, batch_size=self._batch_size)
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
