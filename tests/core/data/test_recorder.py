"""Tests for Recorder: buffering, disk guard, and compaction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pyarrow.parquet as pq
import pytest

from algodesk.api.market import InstrumentId, Tick
from algodesk.data.manifest import DataManifest
from algodesk.data.recorder import Recorder
from algodesk.engine.events import SystemEvent


def _make_tick(inst: str, ts: datetime, ltp: float, vol: int) -> Tick:
    return Tick(
        instrument=InstrumentId(inst),
        exch_ts=ts,
        recv_ts=ts + timedelta(milliseconds=5),
        ltp=ltp,
        ltq=1,
        volume=vol,
    )


def test_recorder_buffering_and_flush(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    manifest_db = tmp_path / "manifest.db"
    manifest = DataManifest(manifest_db)

    recorder = Recorder(
        data_dir=data_dir,
        manifest=manifest,
        max_buffer_rows=2,
    )

    t0 = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    inst = "NSE:EQ:INFY"

    recorder.on_market_event(_make_tick(inst, t0, 100.0, 10))
    # Still in memory
    files = manifest.get_files(instrument=inst)
    assert len(files) == 0

    # Second tick trips max_buffer_rows=2 -> flushes part-0001.parquet
    recorder.on_market_event(_make_tick(inst, t0 + timedelta(seconds=1), 101.0, 20))
    files = manifest.get_files(instrument=inst)
    assert len(files) == 1
    assert files[0].rows == 2
    assert "part-0001.parquet" in files[0].path

    # Third tick in new buffer, flush explicitly -> part-0002.parquet
    recorder.on_market_event(_make_tick(inst, t0 + timedelta(seconds=2), 102.0, 30))
    flushed = recorder.flush()
    assert len(flushed) == 1
    assert "part-0002.parquet" in flushed[0]

    all_files = manifest.get_files(instrument=inst)
    assert len(all_files) == 2


def test_recorder_compaction_and_sorting(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    manifest_db = tmp_path / "manifest.db"
    manifest = DataManifest(manifest_db)

    recorder = Recorder(
        data_dir=data_dir,
        manifest=manifest,
        max_buffer_rows=1,  # every tick creates a part
    )

    inst = "NSE:EQ:INFY"
    # Create two ticks out of order across parts to verify sorting on exch_ts
    t2 = datetime(2026, 10, 26, 9, 15, 2, tzinfo=UTC)
    t1 = datetime(2026, 10, 26, 9, 15, 1, tzinfo=UTC)

    recorder.on_market_event(_make_tick(inst, t2, 102.0, 20))
    recorder.on_market_event(_make_tick(inst, t1, 101.0, 10))

    files_before = manifest.get_files(instrument=inst)
    assert len(files_before) == 2

    # Compact
    compacted = recorder.compact("2026-10-26")
    assert len(compacted) == 1
    assert compacted[0].endswith("day.parquet")

    # Verify manifest updated
    files_after = manifest.get_files(instrument=inst)
    assert len(files_after) == 1
    assert files_after[0].state == "day"
    assert files_after[0].rows == 2

    # Verify day.parquet contents are sorted by exch_ts ascending
    table = pq.read_table(compacted[0])
    assert len(table) == 2
    prices = table.column("ltp").to_pylist()
    assert prices == [101.0, 102.0]


def test_recorder_compaction_failure_keeps_parts(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    manifest_db = tmp_path / "manifest.db"
    manifest = DataManifest(manifest_db)

    recorder = Recorder(
        data_dir=data_dir,
        manifest=manifest,
        max_buffer_rows=1,
    )

    t0 = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    inst = "NSE:EQ:INFY"
    recorder.on_market_event(_make_tick(inst, t0, 100.0, 10))

    pdir = recorder.partition_dir(InstrumentId(inst), "2026-10-26")
    parts = list(pdir.glob("part-*.parquet"))
    assert len(parts) == 1

    # Simulate error during write_table
    with patch("pyarrow.parquet.write_table", side_effect=OSError("Disk write failed")):
        with pytest.raises(IOError, match="Disk write failed"):
            recorder.compact("2026-10-26")

    # Parts must still exist
    assert len(list(pdir.glob("part-*.parquet"))) == 1
    # Day file must not exist
    assert not (pdir / "day.parquet").exists()


def test_recorder_disk_guard(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    manifest_db = tmp_path / "manifest.db"
    manifest = DataManifest(manifest_db)
    events: list[SystemEvent] = []

    recorder = Recorder(
        data_dir=data_dir,
        manifest=manifest,
        min_free_disk_gb=100,  # artificially high threshold
        on_system_event=lambda e: events.append(e),
    )

    # Mock disk_usage to return 10 GB free (< 100 GB required)
    with patch("shutil.disk_usage") as mock_usage:
        mock_usage.return_value = (1000 * 1024**3, 990 * 1024**3, 10 * 1024**3)

        t0 = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
        recorder.on_market_event(_make_tick("NSE:EQ:INFY", t0, 100.0, 10))

        # Recording stopped, event emitted, trading unaffected
        assert len(events) == 1
        assert events[0].kind == "recorder.disk_low"
        assert len(manifest.get_files()) == 0
