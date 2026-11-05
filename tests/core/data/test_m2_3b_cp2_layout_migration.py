"""Tests for M2-3b CP2: Contract-aware layout, property tests, and migration."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pyarrow as pa
import pyarrow.parquet as pq
from hypothesis import given
from hypothesis import strategies as st

from algodesk.api.market import (
    IdParts,
    InstrumentId,
    format_id,
)
from algodesk.data.layout import (
    instrument_partition_path,
    tick_file_path,
)
from algodesk.data.manifest import DataManifest, FileRecord
from algodesk.data.migration import (
    apply_migration,
    build_migration_plan,
    finalize_migration,
)

# --- 1. Property test: no two distinct instruments share a path ---

valid_exchanges = st.sampled_from(["NSE", "BSE", "MCX"])
valid_symbols = st.sampled_from(["RELIANCE", "TCS", "GOLD", "SILVER", "NIFTY"])
valid_dates = st.dates(min_value=date(2025, 1, 1), max_value=date(2027, 12, 31))
valid_strikes = st.sampled_from([Decimal("1000"), Decimal("2000"), Decimal("2500.5")])
valid_option_types = st.sampled_from(["CE", "PE"])


@st.composite
def any_instrument(draw: Any) -> InstrumentId:
    seg = draw(st.sampled_from(["EQ", "FUT", "OPT"]))
    exch = draw(st.sampled_from(["NSE", "BSE", "MCX"]))
    sym = draw(st.sampled_from(["RELIANCE", "TCS", "GOLD", "INFY"]))
    if seg == "EQ":
        return format_id(IdParts(exchange=exch, segment=seg, symbol=sym))
    if seg == "FUT":
        exp = draw(st.dates(min_value=date(2025, 1, 1), max_value=date(2027, 12, 31)))
        return format_id(IdParts(exchange=exch, segment=seg, symbol=sym, expiry=exp))
    exp = draw(st.dates(min_value=date(2025, 1, 1), max_value=date(2027, 12, 31)))
    k = draw(st.sampled_from([Decimal("1000"), Decimal("2000")]))
    opt = draw(st.sampled_from(["CE", "PE"]))
    return format_id(
        IdParts(
            exchange=exch,
            segment=seg,
            symbol=sym,
            expiry=exp,
            strike=k,
            option_type=opt,
        )
    )


@given(i1=any_instrument(), i2=any_instrument())
def test_property_no_distinct_instruments_share_path(i1: InstrumentId, i2: InstrumentId) -> None:
    if i1 == i2:
        return
    p1 = instrument_partition_path(i1)
    p2 = instrument_partition_path(i2)
    assert p1 != p2, f"Collision detected between {i1} and {i2}: {p1}"

    # Path segments contain no spaces or special characters
    for p in (p1, p2):
        for part in p.parts:
            assert " " not in part
            assert ":" not in part


# --- 2. Concrete layout path tests for futures and options ---


def test_futures_and_options_partition_layout(tmp_path: Path) -> None:
    # Future
    fut_id = InstrumentId("MCX:FUT:GOLD:2026-12-04")
    fut_path = tick_file_path(tmp_path, fut_id, date(2026, 10, 28))
    expected_fut = (
        tmp_path
        / "ticks"
        / "exchange=MCX"
        / "segment=FUT"
        / "symbol=GOLD"
        / "expiry=2026-12-04"
        / "date=2026-10-28"
        / "day.parquet"
    )
    assert fut_path == expected_fut

    # Option
    opt_id = InstrumentId("NSE:OPT:NIFTY:2026-12-29:27000:CE")
    opt_path = tick_file_path(tmp_path, opt_id, "2026-10-30")
    expected_opt = (
        tmp_path
        / "ticks"
        / "exchange=NSE"
        / "segment=OPT"
        / "symbol=NIFTY"
        / "expiry=2026-12-29"
        / "strike=27000"
        / "type=CE"
        / "date=2026-10-30"
        / "day.parquet"
    )
    assert opt_path == expected_opt


# --- 3. Migration copy-first, resume, and finalize lifecycle ---


def test_migration_lifecycle(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    manifest_db = data_dir / "state" / "data_manifest.db"
    manifest_db.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(manifest_db)

    # Create dummy legacy tick file
    legacy_dir = (
        data_dir
        / "ticks"
        / "exchange=MCX"
        / "segment=FNO"
        / "symbol=GOLD FUT 04 DEC 26"
        / "date=2026-10-28"
    )
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_file = legacy_dir / "day.parquet"

    # Write dummy parquet
    table = pa.Table.from_arrays(
        [pa.array([1, 2, 3]), pa.array([100.0, 101.0, 102.0])],
        names=["seq", "price"],
    )
    pq.write_table(table, legacy_file)
    content = legacy_file.read_bytes()
    sha = hashlib.sha256(content).hexdigest()

    manifest.add_file(
        FileRecord(
            path=str(legacy_file.resolve()),
            instrument="MCX:FNO:GOLD FUT 04 DEC 26",
            kind="ticks",
            timeframe="tick",
            date="2026-10-28",
            rows=3,
            bytes=len(content),
            first_ts="2026-10-28T09:15:00+00:00",
            last_ts="2026-10-28T15:30:00+00:00",
            sha256=sha,
            source="live",
            state="live",
            created_ts=datetime.now().isoformat(),
        )
    )

    # 1. Build plan (dry run)
    plan = build_migration_plan(data_dir, manifest)
    assert plan.total_files == 1
    item = plan.items[0]
    assert item.canonical_id == InstrumentId("MCX:FUT:GOLD:2026-12-04")
    assert legacy_file.is_file()
    assert not item.target_path.exists()

    # 2. Apply migration (copy-first)
    journal_mock = MagicMock()
    copied = apply_migration(plan, manifest, journal=journal_mock)
    assert copied == 1
    assert item.target_path.is_file()
    assert legacy_file.is_file()  # Old file NOT deleted yet!

    # Verify alias was added to manifest
    assert manifest.resolve_alias("MCX:FNO:GOLD FUT 04 DEC 26") == "MCX:FUT:GOLD:2026-12-04"

    # 3. Finalize migration
    finalized = finalize_migration(plan, manifest, journal=journal_mock)
    assert finalized == 1
    assert item.target_path.is_file()
    assert not legacy_file.exists()  # Old file deleted
    assert not legacy_dir.exists()  # Cleaned up directory
