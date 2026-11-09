"""Tests for vendor file importer and data import CLI command."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from algodesk.api.errors import DataError
from algodesk.api.market import InstrumentId, SeriesKey
from algodesk.cli.app import app
from algodesk.data.importer import (
    compute_file_sha256,
    import_vendor_file,
    load_mapping_file,
)
from algodesk.data.manifest import DataManifest
from algodesk.data.store import BarStore
from algodesk.data.validator import BarValidator


def _create_sample_csv_bars(path: Path) -> Path:
    csv_content = (
        "Date,Open,High,Low,Close,Volume\n"
        "2026-11-04 09:15:00,2500.0,2510.0,2495.0,2505.0,1200\n"
        "2026-11-04 09:16:00,2505.0,2515.0,2502.0,2512.0,850\n"
        "2026-11-04 09:17:00,2512.0,2518.0,2508.0,2510.0,920\n"
    )
    path.write_text(csv_content, encoding="utf-8")
    return path


def _create_sample_mapping(path: Path, kind: str = "bars") -> Path:
    if kind == "bars":
        content = (
            'kind = "bars"\n'
            'timeframe = "1m"\n'
            'timezone = "Asia/Kolkata"\n'
            'symbol = "NSE:EQ:RELIANCE"\n\n'
            "[columns]\n"
            'timestamp = "Date"\n'
            'open = "Open"\n'
            'high = "High"\n'
            'low = "Low"\n'
            'close = "Close"\n'
            'volume = "Volume"\n'
        )
    else:
        content = (
            'kind = "ticks"\n'
            'timezone = "Asia/Kolkata"\n'
            'symbol = "NSE:EQ:RELIANCE"\n\n'
            "[columns]\n"
            'timestamp = "Date"\n'
            'price = "Price"\n'
            'volume = "Volume"\n'
        )
    path.write_text(content, encoding="utf-8")
    return path


def test_compute_sha256_and_load_mapping(tmp_path: Path) -> None:
    csv_file = _create_sample_csv_bars(tmp_path / "sample.csv")
    mapping_file = _create_sample_mapping(tmp_path / "map.toml")

    sha = compute_file_sha256(csv_file)
    assert len(sha) == 64
    mapping = load_mapping_file(mapping_file)
    assert mapping["kind"] == "bars"
    assert mapping["columns"]["timestamp"] == "Date"


def test_import_vendor_csv_bars_success(tmp_project: Any) -> None:
    csv_file = _create_sample_csv_bars(tmp_project / "sample.csv")
    mapping_file = _create_sample_mapping(tmp_project / "map.toml")

    manifest_db = tmp_project / "data" / "state" / "data_manifest.db"
    manifest_db.parent.mkdir(parents=True, exist_ok=True)
    m = DataManifest(manifest_db)
    store = BarStore(tmp_project / "data", manifest=m)
    validator = BarValidator(tmp_project / "data", manifest=m)

    res = import_vendor_file(
        file_path=csv_file,
        mapping_path=mapping_file,
        runtime=tmp_project,
        store=store,
        validator=validator,
        dry_run=False,
    )
    assert res.rows_count == 3
    assert res.instruments == ["NSE:EQ:RELIANCE"]
    assert res.sha256 == compute_file_sha256(csv_file)

    # Verify committed into BarStore
    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="importer",
    )
    start_dt = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
    end_dt = datetime(2026, 11, 4, 23, 59, 59, tzinfo=UTC)
    d_slice = store.read(key, start_dt, end_dt)
    assert len(d_slice.bars) == 3
    m.close()


def test_import_vendor_parquet_bars_success(tmp_project: Any) -> None:
    tbl = pa.table(
        {
            "Date": ["2026-11-04 09:15:00", "2026-11-04 09:16:00"],
            "Open": [100.0, 101.0],
            "High": [105.0, 106.0],
            "Low": [99.0, 100.0],
            "Close": [102.0, 103.0],
            "Volume": [500, 600],
        }
    )
    p_file = tmp_project / "bars.parquet"
    pq.write_table(tbl, p_file)
    map_file = _create_sample_mapping(tmp_project / "map.toml")

    manifest_db = tmp_project / "data" / "state" / "data_manifest.db"
    manifest_db.parent.mkdir(parents=True, exist_ok=True)
    m = DataManifest(manifest_db)
    store = BarStore(tmp_project / "data", manifest=m)
    validator = BarValidator(tmp_project / "data", manifest=m)

    res = import_vendor_file(
        file_path=p_file,
        mapping_path=map_file,
        runtime=tmp_project,
        store=store,
        validator=validator,
        dry_run=False,
    )
    assert res.rows_count == 2
    assert res.timeframe == "1m"
    m.close()


def test_import_vendor_dry_run_no_writes(tmp_project: Any) -> None:
    csv_file = _create_sample_csv_bars(tmp_project / "sample.csv")
    mapping_file = _create_sample_mapping(tmp_project / "map.toml")

    manifest_db = tmp_project / "data" / "state" / "data_manifest.db"
    manifest_db.parent.mkdir(parents=True, exist_ok=True)
    m = DataManifest(manifest_db)
    store = BarStore(tmp_project / "data", manifest=m)
    validator = BarValidator(tmp_project / "data", manifest=m)

    res = import_vendor_file(
        file_path=csv_file,
        mapping_path=mapping_file,
        runtime=tmp_project,
        store=store,
        validator=validator,
        dry_run=True,
    )
    assert res.dry_run is True
    assert res.rows_count == 3
    # Check manifest has no parts
    assert len(m.get_parts()) == 0
    m.close()


def test_import_vendor_ticks_success(tmp_project: Any) -> None:
    ticks_csv = tmp_project / "ticks.csv"
    ticks_csv.write_text(
        "Date,Price,Volume\n2026-11-04 09:15:01,2500.50,10\n2026-11-04 09:15:02,2501.00,20\n",
        encoding="utf-8",
    )
    map_file = _create_sample_mapping(tmp_project / "ticks_map.toml", kind="ticks")

    manifest_db = tmp_project / "data" / "state" / "data_manifest.db"
    manifest_db.parent.mkdir(parents=True, exist_ok=True)
    m = DataManifest(manifest_db)

    res = import_vendor_file(
        file_path=ticks_csv,
        mapping_path=map_file,
        runtime=tmp_project,
        dry_run=False,
    )
    assert res.rows_count == 2
    assert res.kind == "ticks"

    # Verify tick file stored in partition
    tick_files = list((tmp_project / "data" / "ticks").glob("**/ticks.parquet"))
    assert len(tick_files) == 1
    m.close()


def test_import_vendor_validator_failure(tmp_project: Any) -> None:
    # Duplicate timestamps -> BarValidator will reject
    bad_csv = tmp_project / "bad.csv"
    bad_csv.write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2026-11-04 09:15:00,2500.0,2510.0,2490.0,2505.0,1200\n"
        "2026-11-04 09:15:00,2500.0,2510.0,2490.0,2505.0,1200\n",
        encoding="utf-8",
    )
    map_file = _create_sample_mapping(tmp_project / "map.toml")
    manifest_db = tmp_project / "data" / "state" / "data_manifest.db"
    m = DataManifest(manifest_db)
    validator = BarValidator(tmp_project / "data", manifest=m)

    with pytest.raises(DataError) as exc:
        import_vendor_file(
            file_path=bad_csv,
            mapping_path=map_file,
            runtime=tmp_project,
            validator=validator,
        )
    assert "Validation failed" in exc.value.what
    m.close()


def test_cli_data_import_dry_run(tmp_project: Any) -> None:
    csv_file = _create_sample_csv_bars(tmp_project / "sample.csv")
    map_file = _create_sample_mapping(tmp_project / "map.toml")

    runner = CliRunner()
    res = runner.invoke(
        app, ["data", "import", str(csv_file), "--mapping", str(map_file), "--dry-run"]
    )
    assert res.exit_code == 0, f"Output: {res.output}"
    assert "Planned (dry-run)" in res.output
    assert "3 bars row(s)" in res.output
