"""Market event recorder writing partitioned Parquet parts with compaction.

Storage layout:
data/ticks/exchange=E/segment=S/symbol=X/date=D/part-0001.parquet
data/ticks/exchange=E/segment=S/symbol=X/date=D/day.parquet
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from algodesk.api.market import FeedStatus, InstrumentId, MarketEvent, Quote, Tick, parse_id
from algodesk.data.manifest import DataManifest, FileRecord
from algodesk.data.schema import events_to_table

logger = logging.getLogger("algodesk.data.recorder")


@dataclass(frozen=True, slots=True)
class RecorderEvent:
    """System event emitted by the recorder (e.g. disk low)."""

    kind: str
    message: str
    ts: datetime


def _compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class Recorder:
    """Buffers market events, flushes part files, guards disk, and compacts day files."""

    __slots__ = (
        "_buffers",
        "_bus",
        "_data_dir",
        "_disk_halted",
        "_flush_seconds",
        "_manifest",
        "_max_buffer_rows",
        "_min_free_disk_gb",
        "_on_system_event",
        "_part_seq",
        "_scheduler",
        "_source",
    )

    def __init__(
        self,
        data_dir: Path | str,
        manifest: DataManifest | None = None,
        bus: Any = None,
        scheduler: Any = None,
        flush_seconds: int | None = None,
        max_buffer_rows: int | None = None,
        min_free_disk_gb: int | None = None,
        source: str = "live",
        on_system_event: Callable[[Any], None] | None = None,
    ) -> None:

        self._data_dir = Path(data_dir)
        self._manifest = manifest
        self._bus = bus
        self._scheduler = scheduler
        self._flush_seconds = flush_seconds if flush_seconds is not None else 60
        self._max_buffer_rows = max_buffer_rows if max_buffer_rows is not None else 5000
        self._min_free_disk_gb = min_free_disk_gb if min_free_disk_gb is not None else 5
        self._source = source
        self._on_system_event = on_system_event

        self._buffers: dict[InstrumentId, list[Tick | Quote]] = {}
        self._part_seq: dict[tuple[InstrumentId, str], int] = {}
        self._disk_halted = False

        if self._bus is not None:
            self._bus.subscribe(MarketEvent, self.on_market_event, critical=False)

        if self._scheduler is not None:
            now = self._scheduler.clock.now()
            self._scheduler.schedule_repeating(
                "recorder.flush",
                now,
                float(self._flush_seconds),
                # pyrefly: ignore[implicit-any-lambda]
                lambda *_: self.flush(),
            )

    def on_market_event(self, event: MarketEvent) -> None:
        """Buffer incoming market event with isolated error handling and disk guard."""
        try:
            if isinstance(event, FeedStatus):
                return
            if not self._check_disk():
                return
            inst = event.instrument
            buf = self._buffers.setdefault(inst, [])
            buf.append(event)
            if len(buf) >= self._max_buffer_rows:
                self._flush_instrument(inst)
        except Exception:
            pass  # Never block trading on recording error

    def _check_disk(self) -> bool:
        """Check free disk space against min_free_disk_gb."""
        try:
            usage = shutil.disk_usage(self._data_dir if self._data_dir.exists() else Path("."))
            free_bytes = getattr(usage, "free", usage[2] if len(usage) > 2 else 0)
            free_gb = free_bytes / (1024**3)
            if free_gb < self._min_free_disk_gb:
                if not self._disk_halted:
                    self._disk_halted = True
                    msg = (
                        f"Free disk ({free_gb:.1f} GB) below limit "
                        f"({self._min_free_disk_gb} GB); recording halted"
                    )
                    self._emit_system_event(
                        kind="recorder.disk_low",
                        message=msg,
                    )
                return False
            return True
        except Exception:
            return True

    def _emit_system_event(self, kind: str, message: str) -> None:
        evt = RecorderEvent(kind=kind, message=message, ts=datetime.now(UTC))
        if self._bus is not None and hasattr(self._bus, "publish"):
            self._bus.publish(evt)
        if self._on_system_event is not None:
            self._on_system_event(evt)

    def partition_dir(self, instrument: InstrumentId, date_str: str) -> Path:
        """Compute partitioned directory path for ticks."""
        parts = parse_id(instrument)
        return (
            self._data_dir
            / "ticks"
            / f"exchange={parts.exchange}"
            / f"segment={parts.segment}"
            / f"symbol={parts.symbol}"
            / f"date={date_str}"
        )

    def flush(self, instrument: InstrumentId | None = None) -> list[str]:
        """Flush buffer to part file(s) and record in manifest."""
        written: list[str] = []
        try:
            targets = [instrument] if instrument else list(self._buffers.keys())
            for inst in targets:
                path = self._flush_instrument(inst)
                if path:
                    written.append(path)
        except Exception as exc:
            logger.warning("Recorder flush failed: %s", exc)
        return written

    def _flush_instrument(self, instrument: InstrumentId) -> str | None:
        buf = self._buffers.pop(instrument, [])
        if not buf:
            return None

        d_str = buf[0].exch_ts.date().isoformat()
        pdir = self.partition_dir(instrument, d_str)
        pdir.mkdir(parents=True, exist_ok=True)

        key = (instrument, d_str)
        seq = self._part_seq.get(key, 0) + 1
        self._part_seq[key] = seq
        filename = f"part-{seq:04d}.parquet"
        file_path = pdir / filename

        table = events_to_table(buf)
        pq.write_table(table, file_path, compression="zstd")

        size_bytes = file_path.stat().st_size
        sha256 = _compute_sha256(file_path)
        first_ts = buf[0].exch_ts.isoformat()
        last_ts = buf[-1].exch_ts.isoformat()
        created_ts = datetime.now(UTC).isoformat()

        rec = FileRecord(
            path=file_path.as_posix(),
            instrument=str(instrument),
            kind="ticks",
            timeframe="tick",
            date=d_str,
            rows=len(buf),
            bytes=size_bytes,
            first_ts=first_ts,
            last_ts=last_ts,
            sha256=sha256,
            source=self._source,
            state="part",
            created_ts=created_ts,
        )
        if self._manifest is not None:
            self._manifest.record_file(rec)
        return file_path.as_posix()

    def compact(self, date_val: str | date) -> list[str]:
        """Merge parts of each instrument-day into day.parquet sorted by (exch_ts, recv_ts)."""
        d_str = date_val.isoformat() if isinstance(date_val, date) else date_val
        ticks_dir = self._data_dir / "ticks"
        if not ticks_dir.exists():
            return []

        compacted: list[str] = []
        for date_dir in ticks_dir.glob(f"*/*/*/date={d_str}"):
            res = self._compact_partition(date_dir, d_str)
            if res:
                compacted.append(res)
        return compacted

    def _compact_partition(self, date_dir: Path, d_str: str) -> str | None:
        parts = sorted(date_dir.glob("part-*.parquet"))
        if not parts:
            return None

        tables: list[pa.Table] = []
        total_rows = 0
        part_paths = [p.as_posix() for p in parts]

        for p in parts:
            t = pq.read_table(p)
            total_rows += len(t)
            tables.append(t)

        combined = pa.concat_tables(tables)
        if len(combined) != total_rows:
            msg = (
                f"Compaction row count mismatch in {date_dir}: "
                f"expected {total_rows}, got {len(combined)}"
            )
            raise RuntimeError(msg)

        # Sort by (exch_ts, recv_ts)
        sorted_indices = pa.compute.sort_indices(
            combined,
            sort_keys=[("exch_ts", "ascending"), ("recv_ts", "ascending")],
        )
        sorted_table = combined.take(sorted_indices)

        tmp_file = date_dir / "day.parquet.tmp"
        day_file = date_dir / "day.parquet"

        try:
            pq.write_table(sorted_table, tmp_file, compression="zstd")
            written_table = pq.read_table(tmp_file)
            if len(written_table) != total_rows:
                raise RuntimeError(f"Verified rows mismatch in {tmp_file}")

            sha256 = _compute_sha256(tmp_file)
            size_bytes = tmp_file.stat().st_size
            tmp_file.replace(day_file)
        except Exception:
            if tmp_file.exists():
                tmp_file.unlink()
            raise

        for p in parts:
            try:
                p.unlink()
            except OSError:
                pass

        inst_str = self._derive_instrument_from_path(date_dir)
        first_ts = sorted_table["exch_ts"][0].as_py().isoformat()
        last_ts = sorted_table["exch_ts"][-1].as_py().isoformat()

        day_rec = FileRecord(
            path=day_file.as_posix(),
            instrument=inst_str,
            kind="ticks",
            timeframe="tick",
            date=d_str,
            rows=total_rows,
            bytes=size_bytes,
            first_ts=first_ts,
            last_ts=last_ts,
            sha256=sha256,
            source=self._source,
            state="day",
            created_ts=datetime.now(UTC).isoformat(),
        )
        if self._manifest is not None:
            self._manifest.replace_parts_with_day(inst_str, d_str, part_paths, day_rec)
        return day_file.as_posix()

    def _derive_instrument_from_path(self, pdir: Path) -> str:
        parts = pdir.parts
        exch, seg, sym = "", "", ""
        for p in parts:
            if p.startswith("exchange="):
                exch = p.split("=")[1]
            elif p.startswith("segment="):
                seg = p.split("=")[1]
            elif p.startswith("symbol="):
                sym = p.split("=")[1]
        return f"{exch}:{seg}:{sym}".upper()


MarketRecorder = Recorder
