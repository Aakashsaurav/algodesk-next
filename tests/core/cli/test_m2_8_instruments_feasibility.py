"""Tests for M2-8 CP1 Step 5: Instrument feasibility logic, lot calculations, and spaced IDs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
from typer.testing import CliRunner

from algodesk.api.market import Bar, InstrumentId, SeriesKey
from algodesk.cli import app
from algodesk.cli.instruments import _find_latest_close_price
from algodesk.data.layout import instrument_partition_path
from algodesk.data.manifest import DataManifest
from algodesk.data.store import BarStore

_IST = ZoneInfo("Asia/Kolkata")


def test_feasibility_path_encoding_of_id_with_spaces() -> None:
    """Instrument IDs containing spaces (e.g. NSE:INDEX:NIFTY 50) encode spaces as underscores."""
    part = instrument_partition_path("NSE:INDEX:NIFTY 50")
    # Path components must be sanitized with underscores
    assert "symbol=NIFTY_50" in str(part)
    assert "segment=INDEX" in str(part)
    assert "exchange=NSE" in str(part)


def test_find_latest_close_price_from_spaced_id_file(tmp_project: Path) -> None:
    """_find_latest_close_price finds latest close price for instrument with spaces in ID."""
    manifest = DataManifest(tmp_project / "data" / "manifest.db")
    part_rel = instrument_partition_path("NSE:INDEX:NIFTY 50")
    parquet_path = (
        tmp_project
        / "data"
        / "bars"
        / "source=nse_bhavcopy"
        / "timeframe=1d"
        / part_rel
        / "adj=raw"
        / "daily.parquet"
    )
    parquet_path.parent.mkdir(parents=True, exist_ok=True)

    tbl = pa.Table.from_pydict(
        {
            "start_ts": [
                datetime(2024, 10, 25, 9, 15, tzinfo=UTC),
                datetime(2024, 10, 28, 9, 15, tzinfo=UTC),
            ],
            "close": [24180.80, 24339.15],
        }
    )
    pq.write_table(tbl, parquet_path)

    # Check that _find_latest_close_price successfully resolves the spaced ID
    px = _find_latest_close_price(manifest, tmp_project / "data", "NSE:INDEX:NIFTY 50")
    assert px is not None
    assert px == Decimal("24339.15")


def test_feasibility_lot_value_and_fits_yes(tmp_project: Path) -> None:
    """Instruments whose lot value <= allocated capital receive fits='YES'."""
    # Write group
    group_file = tmp_project / "config" / "groups" / "test_fit.toml"
    group_file.parent.mkdir(parents=True, exist_ok=True)
    group_file.write_text("name = 'test_fit'\nmembers = ['NSE:EQ:AFFORD']\n", encoding="utf-8")

    manifest = DataManifest(tmp_project / "data" / "manifest.db")
    bar_store = BarStore(tmp_project / "data", manifest, exchange_tz=_IST)

    dt = datetime(2024, 10, 28, 9, 15, tzinfo=UTC)
    inst = InstrumentId("NSE:EQ:AFFORD")
    b = Bar(inst, "1d", dt, dt, 100.0, 105.0, 95.0, 100.0, 1000, True, False)
    key = SeriesKey(inst, "bars", "1d", "raw", "nse_bhavcopy")
    bar_store.commit(key, [b])

    runner = CliRunner()
    res = runner.invoke(
        app, ["instruments", "feasibility", "-g", "test_fit", "-c", "5000", "--json"]
    )
    assert res.exit_code == 0
    json_str = res.output[res.output.find("{") :]
    data = json.loads(json_str)
    item = data["instruments"][0]
    assert item["instrument"] == "NSE:EQ:AFFORD"
    assert item["lot_size"] == 1
    assert item["close_price"] == 100.0
    assert item["lot_value"] == 100.0
    assert item["fits"] == "YES"


def test_feasibility_lot_value_and_fits_no(tmp_project: Path) -> None:
    """Instruments whose lot value > allocated capital receive fits='NO'."""
    group_file = tmp_project / "config" / "groups" / "test_expensive.toml"
    group_file.parent.mkdir(parents=True, exist_ok=True)
    group_file.write_text(
        "name = 'test_expensive'\nmembers = ['NSE:EQ:EXPENSIVE']\n", encoding="utf-8"
    )

    manifest = DataManifest(tmp_project / "data" / "manifest.db")
    bar_store = BarStore(tmp_project / "data", manifest, exchange_tz=_IST)

    dt = datetime(2024, 10, 28, 9, 15, tzinfo=UTC)
    inst = InstrumentId("NSE:EQ:EXPENSIVE")
    b = Bar(inst, "1d", dt, dt, 10000.0, 10050.0, 9950.0, 10000.0, 100, True, False)
    key = SeriesKey(inst, "bars", "1d", "raw", "nse_bhavcopy")
    bar_store.commit(key, [b])

    runner = CliRunner()
    res = runner.invoke(
        app, ["instruments", "feasibility", "-g", "test_expensive", "-c", "5000", "--json"]
    )
    assert res.exit_code == 0
    json_str = res.output[res.output.find("{") :]
    data = json.loads(json_str)
    item = data["instruments"][0]
    assert item["fits"] == "NO"
    assert item["lot_value"] == 10000.0


def test_feasibility_no_price_case(tmp_project: Path) -> None:
    """Instruments without recorded close price receive fits='UNKNOWN' and 'no price'."""
    group_file = tmp_project / "config" / "groups" / "test_noprice.toml"
    group_file.parent.mkdir(parents=True, exist_ok=True)
    group_file.write_text("name = 'test_noprice'\nmembers = ['NSE:EQ:NOPRICE']\n", encoding="utf-8")

    runner = CliRunner()
    res = runner.invoke(
        app, ["instruments", "feasibility", "-g", "test_noprice", "-c", "5000", "--json"]
    )
    assert res.exit_code == 0
    json_str = res.output[res.output.find("{") :]
    data = json.loads(json_str)
    item = data["instruments"][0]
    assert item["fits"] == "UNKNOWN"
    assert item["close_price"] is None
    assert item["lot_value"] is None

    # Text output check
    res_txt = runner.invoke(app, ["instruments", "feasibility", "-g", "test_noprice", "-c", "5000"])
    assert res_txt.exit_code == 0
    assert "no price" in res_txt.output
    assert "UNKNOWN" in res_txt.output


def test_feasibility_unknown_group_refuses(tmp_project: Path) -> None:
    """Nonexistent group name raises ConfigError with available groups."""
    runner = CliRunner()
    res = runner.invoke(
        app, ["instruments", "feasibility", "-g", "nonexistent_group", "-c", "5000"]
    )
    assert res.exit_code != 0
    assert "Instrument group 'nonexistent_group' not found" in res.output


def test_feasibility_with_nifty_spaced_id_in_group(tmp_project: Path) -> None:
    """Instrument group with NSE:INDEX:NIFTY 50 resolves close and feasibility."""
    group_file = tmp_project / "config" / "groups" / "test_nifty.toml"
    group_file.parent.mkdir(parents=True, exist_ok=True)
    group_file.write_text(
        "name = 'test_nifty'\nmembers = ['NSE:INDEX:NIFTY 50']\n", encoding="utf-8"
    )

    part_rel = instrument_partition_path("NSE:INDEX:NIFTY 50")
    parquet_path = (
        tmp_project
        / "data"
        / "bars"
        / "source=nse_bhavcopy"
        / "timeframe=1d"
        / part_rel
        / "adj=raw"
        / "daily.parquet"
    )
    parquet_path.parent.mkdir(parents=True, exist_ok=True)

    tbl = pa.Table.from_pydict(
        {
            "start_ts": [datetime(2024, 10, 28, 9, 15, tzinfo=UTC)],
            "close": [24339.15],
        }
    )
    pq.write_table(tbl, parquet_path)

    runner = CliRunner()
    res = runner.invoke(
        app, ["instruments", "feasibility", "-g", "test_nifty", "-c", "50000", "--json"]
    )
    assert res.exit_code == 0
    json_str = res.output[res.output.find("{") :]
    data = json.loads(json_str)
    item = data["instruments"][0]
    assert item["instrument"] == "NSE:INDEX:NIFTY 50"
    assert item["close_price"] == 24339.15
    assert item["fits"] == "YES"
