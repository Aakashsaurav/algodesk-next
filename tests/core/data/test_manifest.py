"""Tests for SQLite DataManifest: transactions, file records, and instruments."""

from __future__ import annotations

from pathlib import Path

from algodesk.data.manifest import DataManifest, FileRecord, InstrumentRecord


def test_manifest_file_and_instrument_lifecycle(tmp_path: Path) -> None:
    db_file = tmp_path / "data_manifest.db"
    manifest = DataManifest(db_file)

    rec = FileRecord(
        path="data/ticks/exchange=NSE/segment=EQ/symbol=INFY/date=2026-10-26/part-0001.parquet",
        instrument="NSE:EQ:INFY",
        kind="ticks",
        timeframe="tick",
        date="2026-10-26",
        rows=100,
        bytes=1024,
        first_ts="2026-10-26T09:15:00+00:00",
        last_ts="2026-10-26T09:16:00+00:00",
        sha256="abc123hash",
        source="live",
        state="part",
        created_ts="2026-10-26T09:16:05+00:00",
    )
    manifest.record_file(rec)

    files = manifest.get_files(instrument="NSE:EQ:INFY")
    assert len(files) == 1
    assert files[0].rows == 100
    assert files[0].state == "part"

    # Replace part with day.parquet
    day_rec = FileRecord(
        path="data/ticks/exchange=NSE/segment=EQ/symbol=INFY/date=2026-10-26/day.parquet",
        instrument="NSE:EQ:INFY",
        kind="ticks",
        timeframe="tick",
        date="2026-10-26",
        rows=100,
        bytes=950,
        first_ts="2026-10-26T09:15:00+00:00",
        last_ts="2026-10-26T09:16:00+00:00",
        sha256="dayhash456",
        source="live",
        state="day",
        created_ts="2026-10-26T15:35:00+00:00",
    )
    manifest.replace_parts_with_day("NSE:EQ:INFY", "2026-10-26", [rec.path], day_rec)

    current_files = manifest.get_files(instrument="NSE:EQ:INFY")
    assert len(current_files) == 1
    assert current_files[0].path == day_rec.path
    assert current_files[0].state == "day"

    # Instruments
    inst = InstrumentRecord(
        id="NSE:EQ:INFY",
        exchange="NSE",
        segment="EQ",
        trading_symbol="INFY",
        kind="equity",
    )
    manifest.record_instruments([inst])
    all_insts = manifest.get_instruments()
    assert len(all_insts) == 1
    assert all_insts[0].trading_symbol == "INFY"

    # Delete
    deleted = manifest.delete_files([day_rec.path])
    assert deleted == 1
    assert len(manifest.get_files(instrument="NSE:EQ:INFY")) == 0

    manifest.close()
