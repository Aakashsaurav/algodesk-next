"""Tests for CP0: SeriesKey model, bar layout, parquet schema, and manifest migration."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq
import pytest

from algodesk.api.errors import DataError
from algodesk.api.market import (
    Bar,
    CoverageStatus,
    DataGap,
    DataSlice,
    InstrumentId,
    SeriesKey,
)
from algodesk.data.layout import (
    bar_file_path,
    dataset_id_for_key,
    parse_bar_file_path,
)
from algodesk.data.manifest import (
    CoverageDayRecord,
    DataManifest,
    DatasetRecord,
    PartRecord,
    QuarantineRecord,
)
from algodesk.data.schema import (
    BAR_SCHEMA,
    bars_to_table,
    read_bars_parquet,
    table_to_bars,
    write_bars_parquet,
)
from algodesk.params.defaults import load_defaults


def test_series_key_actions_and_snapshot_kinds() -> None:
    """SeriesKey accepts actions and snapshot kinds, and validates raw/adjusted."""
    key_act = SeriesKey(
        instrument=InstrumentId("NSE:EQ:INFY"),
        kind="actions",
        timeframe="1d",
        adjustment="raw",
        source="yfinance",
    )
    assert key_act.kind == "actions"

    key_snap = SeriesKey(
        instrument=InstrumentId("NSE:EQ:INFY"),
        kind="snapshot",
        timeframe="1d",
        adjustment="adjusted",
        source="upstox",
    )
    assert key_snap.kind == "snapshot"

    with pytest.raises(ValueError, match="Invalid series kind"):
        SeriesKey(
            instrument=InstrumentId("NSE:EQ:INFY"),
            kind="custom_kind",
            timeframe="1m",
            adjustment="raw",
            source="upstox",
        )

    with pytest.raises(ValueError, match="Invalid adjustment"):
        SeriesKey(
            instrument=InstrumentId("NSE:EQ:INFY"),
            kind="bars",
            timeframe="1m",
            adjustment="unadjusted",
            source="upstox",
        )


def test_coverage_status_and_data_slice_types() -> None:
    """CoverageStatus and DataSlice types satisfy API contracts."""
    assert CoverageStatus.COMPLETE == "COMPLETE"
    assert CoverageStatus.PARTIAL == "PARTIAL"
    assert CoverageStatus.CLOSED == "CLOSED"
    assert CoverageStatus.EMPTY == "EMPTY"
    assert CoverageStatus.QUARANTINED == "QUARANTINED"
    assert CoverageStatus.UNAVAILABLE == "UNAVAILABLE"

    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:TCS"),
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="upstox",
    )
    start_dt = datetime(2026, 10, 26, 3, 45, tzinfo=UTC)
    end_dt = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)
    bar = Bar(
        instrument=key.instrument,
        timeframe="1m",
        start_ts=start_dt,
        end_ts=datetime(2026, 10, 26, 3, 46, tzinfo=UTC),
        open=3500.0,
        high=3510.0,
        low=3495.0,
        close=3505.0,
        volume=100,
        complete=True,
    )
    gap = DataGap(day=date(2026, 10, 27), status=CoverageStatus.EMPTY, reason="No data from source")
    slice_obj = DataSlice(
        key=key,
        start=start_dt,
        end=end_dt,
        bars=(bar,),
        gaps=(gap,),
        dataset_ids=("bars:upstox:1m:NSE:EQ:TCS:raw",),
        checksums=("sha256dummy",),
    )
    assert slice_obj.has_gaps is True
    assert slice_obj.total_bars == 1
    assert slice_obj.dataset_ids == ("bars:upstox:1m:NSE:EQ:TCS:raw",)


def test_data_parameters_in_registry() -> None:
    """All M2 data parameters exist in registry with correct defaults and constraints."""
    specs = {s.name: s for s in load_defaults(Path("config/params"))}

    assert "data.base_timeframes" in specs
    assert specs["data.base_timeframes"].default == "1m,1d"

    assert "data.settle_minutes" in specs
    assert specs["data.settle_minutes"].default == 15

    assert "data.quarantine_days" in specs
    assert specs["data.quarantine_days"].default == 3

    assert "data.session_bars_keep_days" in specs
    assert specs["data.session_bars_keep_days"].default == 1

    assert "data.price_jump_pct" in specs
    assert specs["data.price_jump_pct"].default == 20.0


def test_bar_layout_paths(tmp_path: Path) -> None:
    """Layout generates exact required paths for 1m and 1d bars."""
    key_1m = SeriesKey(
        instrument=InstrumentId("NSE:EQ:HDFCBANK"),
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="upstox",
    )
    dt_1m = datetime(2026, 10, 29, 9, 15, tzinfo=UTC)
    path_1m = bar_file_path(tmp_path, key_1m, dt_1m)
    expected_1m = (
        tmp_path
        / "bars"
        / "source=upstox"
        / "timeframe=1m"
        / "exchange=NSE"
        / "segment=EQ"
        / "symbol=HDFCBANK"
        / "adj=raw"
        / "year=2026"
        / "2026-10.parquet"
    )
    assert path_1m == expected_1m

    key_1d = SeriesKey(
        instrument=InstrumentId("NSE:EQ:HDFCBANK"),
        kind="bars",
        timeframe="1d",
        adjustment="adjusted",
        source="yfinance",
    )
    path_1d = bar_file_path(tmp_path, key_1d, dt_1m)
    expected_1d = (
        tmp_path
        / "bars"
        / "source=yfinance"
        / "timeframe=1d"
        / "exchange=NSE"
        / "segment=EQ"
        / "symbol=HDFCBANK"
        / "adj=adjusted"
        / "daily.parquet"
    )
    assert path_1d == expected_1d

    # Invalid timeframe must raise DataError
    key_5m = SeriesKey(
        instrument=InstrumentId("NSE:EQ:HDFCBANK"),
        kind="bars",
        timeframe="5m",
        adjustment="raw",
        source="upstox",
    )
    with pytest.raises(DataError, match="Unsupported bar timeframe"):
        bar_file_path(tmp_path, key_5m, dt_1m)


def test_parse_bar_file_path(tmp_path: Path) -> None:
    """parse_bar_file_path correctly inverts 1m and 1d file paths."""
    p_1m = (
        tmp_path
        / "bars"
        / "source=upstox"
        / "timeframe=1m"
        / "exchange=NSE"
        / "segment=EQ"
        / "symbol=INFY"
        / "adj=raw"
        / "year=2026"
        / "2026-10.parquet"
    )
    info_1m = parse_bar_file_path(p_1m)
    assert info_1m is not None
    assert info_1m["source"] == "upstox"
    assert info_1m["timeframe"] == "1m"
    assert info_1m["exchange"] == "NSE"
    assert info_1m["segment"] == "EQ"
    assert info_1m["symbol"] == "INFY"
    assert info_1m["adj"] == "raw"
    assert info_1m["year"] == 2026
    assert info_1m["month"] == 10

    p_1d = (
        tmp_path
        / "bars"
        / "source=yfinance"
        / "timeframe=1d"
        / "exchange=NSE"
        / "segment=EQ"
        / "symbol=RELIANCE"
        / "adj=adjusted"
        / "daily.parquet"
    )
    info_1d = parse_bar_file_path(p_1d)
    assert info_1d is not None
    assert info_1d["source"] == "yfinance"
    assert info_1d["timeframe"] == "1d"
    assert info_1d["symbol"] == "RELIANCE"
    assert info_1d["adj"] == "adjusted"
    assert info_1d["year"] is None


def test_dataset_id_for_key() -> None:
    """dataset_id_for_key formats deterministic identifier."""
    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:SBIN"),
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="upstox",
    )
    assert dataset_id_for_key(key) == "bars:upstox:1m:NSE:EQ:SBIN:raw"


def test_parquet_zstd_read_write_roundtrip(tmp_path: Path) -> None:
    """Parquet files write with zstd compression, UTC timestamps, and exchange-time column."""
    ist = ZoneInfo("Asia/Kolkata")
    start_1 = datetime(2026, 10, 26, 3, 45, tzinfo=UTC)
    end_1 = datetime(2026, 10, 26, 3, 46, tzinfo=UTC)
    start_2 = datetime(2026, 10, 26, 3, 46, tzinfo=UTC)
    end_2 = datetime(2026, 10, 26, 3, 47, tzinfo=UTC)

    inst_id = InstrumentId("NSE:EQ:INFY")
    bars = [
        Bar(
            instrument=inst_id,
            timeframe="1m",
            start_ts=start_1,
            end_ts=end_1,
            open=1450.0,
            high=1455.0,
            low=1448.0,
            close=1452.0,
            volume=500,
            complete=True,
            synthetic=False,
        ),
        Bar(
            instrument=inst_id,
            timeframe="1m",
            start_ts=start_2,
            end_ts=end_2,
            open=1452.0,
            high=1456.0,
            low=1451.0,
            close=1454.0,
            volume=350,
            complete=True,
            synthetic=False,
        ),
    ]

    # Verify bars_to_table and table_to_bars with BAR_SCHEMA
    tbl = bars_to_table(bars, exchange_tz=ist)
    assert tbl.schema.equals(BAR_SCHEMA)
    reconverted = table_to_bars(tbl, inst_id, "1m")
    assert len(reconverted) == 2
    assert reconverted[0].open == 1450.0

    out_file = tmp_path / "test_bars.parquet"
    write_bars_parquet(out_file, bars, exchange_tz=ist)

    assert out_file.exists()

    # Verify parquet metadata and compression
    parquet_file = pq.ParquetFile(out_file)
    assert parquet_file.metadata.num_rows == 2
    for rg in range(parquet_file.metadata.num_row_groups):
        col_meta = parquet_file.metadata.row_group(rg).column(0)
        assert col_meta.compression.lower() == "zstd"

    # Read back and verify schema and contents
    loaded = read_bars_parquet(out_file, inst_id, timeframe="1m")
    assert len(loaded) == 2
    assert loaded[0].start_ts == start_1
    assert loaded[0].close == 1452.0
    assert loaded[0].volume == 500
    assert loaded[1].start_ts == start_2
    assert loaded[1].close == 1454.0
    assert loaded[1].volume == 350


def test_manifest_schema_version_and_migration(tmp_path: Path) -> None:
    """Fresh database initializes at version 2; pre-existing v1 migrates to v2."""
    db_file = tmp_path / "fresh_manifest.db"
    m_fresh = DataManifest(db_file)
    assert m_fresh.schema_version == 2
    m_fresh.close()

    # Simulate pre-existing v1 database with only files and instruments
    v1_file = tmp_path / "v1_manifest.db"
    conn = sqlite3.connect(str(v1_file))
    conn.execute("""
        CREATE TABLE files (
            path TEXT PRIMARY KEY,
            instrument TEXT NOT NULL,
            kind TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            date TEXT NOT NULL,
            rows INTEGER NOT NULL,
            bytes INTEGER NOT NULL,
            first_ts TEXT NOT NULL,
            last_ts TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            source TEXT NOT NULL,
            state TEXT NOT NULL,
            created_ts TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE instruments (
            id TEXT PRIMARY KEY,
            exchange TEXT NOT NULL,
            segment TEXT NOT NULL,
            trading_symbol TEXT NOT NULL,
            kind TEXT NOT NULL,
            underlying TEXT,
            expiry TEXT
        )
    """)
    conn.commit()
    conn.close()

    m_migrated = DataManifest(v1_file)
    assert m_migrated.schema_version == 2
    m_migrated.close()


def test_manifest_datasets_crud(tmp_path: Path) -> None:
    """DataManifest records and queries datasets under transactions."""
    m = DataManifest(tmp_path / "manifest.db")
    rec = DatasetRecord(
        dataset_id="bars:upstox:1m:NSE:EQ:INFY:raw",
        instrument="NSE:EQ:INFY",
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="upstox",
        schema_version=1,
        created_at="2026-10-29T10:00:00Z",
    )
    m.record_dataset(rec)

    fetched = m.get_dataset("bars:upstox:1m:NSE:EQ:INFY:raw")
    assert fetched is not None
    assert fetched.instrument == "NSE:EQ:INFY"
    assert fetched.source == "upstox"

    datasets = m.list_datasets(source="upstox")
    assert len(datasets) == 1
    assert datasets[0].dataset_id == rec.dataset_id
    m.close()


def test_manifest_parts_crud(tmp_path: Path) -> None:
    """DataManifest records, queries, and deletes parts under transactions."""
    m = DataManifest(tmp_path / "manifest.db")
    ds_rec = DatasetRecord(
        dataset_id="bars:upstox:1m:NSE:EQ:TCS:raw",
        instrument="NSE:EQ:TCS",
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="upstox",
        created_at="2026-10-29T10:00:00Z",
    )
    m.record_dataset(ds_rec)

    part = PartRecord(
        path="data/bars/source=upstox/timeframe=1m/exchange=NSE/segment=EQ/symbol=TCS/adj=raw/year=2026/2026-10.parquet",
        dataset_id="bars:upstox:1m:NSE:EQ:TCS:raw",
        rows=1000,
        bytes=25000,
        first_ts="2026-10-01T03:45:00Z",
        last_ts="2026-10-29T10:00:00Z",
        sha256="partsha256test",
        source="upstox",
        source_version="1.0",
        fetched_at="2026-10-29T10:05:00Z",
    )
    m.record_part(part)

    parts = m.get_parts(dataset_id="bars:upstox:1m:NSE:EQ:TCS:raw")
    assert len(parts) == 1
    assert parts[0].rows == 1000
    assert parts[0].sha256 == "partsha256test"

    p_by_path = m.get_part_by_path(part.path)
    assert p_by_path is not None
    assert p_by_path.dataset_id == part.dataset_id

    m.delete_part(part.path)
    assert m.get_part_by_path(part.path) is None
    m.close()


def test_manifest_coverage_days_crud(tmp_path: Path) -> None:
    """DataManifest records single and batch coverage days with queries."""
    m = DataManifest(tmp_path / "manifest.db")
    ds_id = "bars:upstox:1m:NSE:EQ:WIPRO:raw"
    m.record_dataset(
        DatasetRecord(
            dataset_id=ds_id,
            instrument="NSE:EQ:WIPRO",
            kind="bars",
            timeframe="1m",
            adjustment="raw",
            source="upstox",
            created_at="2026-10-29T10:00:00Z",
        )
    )

    day1 = CoverageDayRecord(
        dataset_id=ds_id,
        day="2026-10-26",
        status=CoverageStatus.COMPLETE,
        rows=375,
        expected_rows=375,
        part_path="dummy_path.parquet",
        fetched_at="2026-10-26T16:00:00Z",
        note="",
    )
    day2 = CoverageDayRecord(
        dataset_id=ds_id,
        day="2026-10-27",
        status=CoverageStatus.PARTIAL,
        rows=100,
        expected_rows=375,
        part_path="dummy_path.parquet",
        fetched_at="2026-10-27T11:00:00Z",
        note="session open",
    )
    m.record_coverage_days([day1, day2])

    records = m.get_coverage_days(ds_id)
    assert len(records) == 2
    assert records[0].status == CoverageStatus.COMPLETE
    assert records[1].status == CoverageStatus.PARTIAL

    filtered = m.get_coverage_days(ds_id, start_day="2026-10-27", end_day="2026-10-27")
    assert len(filtered) == 1
    assert filtered[0].day == "2026-10-27"
    m.close()


def test_manifest_fetch_log_and_quarantine(tmp_path: Path) -> None:
    """DataManifest logs fetches and tracks quarantined chunks."""
    m = DataManifest(tmp_path / "manifest.db")

    log_id = m.record_fetch_log(
        chunk="NSE:EQ:INFY 2026-10-20 to 2026-10-25",
        source="upstox",
        started="2026-10-29T10:00:00Z",
        finished="2026-10-29T10:00:02Z",
        rows=1500,
        outcome="success",
        error="",
    )
    assert log_id > 0
    logs = m.get_fetch_logs()
    assert len(logs) == 1
    assert logs[0].outcome == "success"

    q_rec = QuarantineRecord(
        id="q_12345",
        chunk="NSE:EQ:BAD 2026-10-26",
        reason="OHLC sanity check failed: low > high",
        report_path="data/.quarantine/20261029_100000/report.json",
        created="2026-10-29T10:00:00Z",
        status="quarantined",
    )
    m.record_quarantine(q_rec)

    items = m.get_quarantine(status="quarantined")
    assert len(items) == 1
    assert items[0].id == "q_12345"

    m.update_quarantine("q_12345", status="released", released_at="2026-10-29T10:30:00Z")
    assert len(m.get_quarantine(status="quarantined")) == 0
    released = m.get_quarantine(id="q_12345")
    assert len(released) == 1
    assert released[0].status == "released"
    assert released[0].released_at == "2026-10-29T10:30:00Z"
    m.close()
