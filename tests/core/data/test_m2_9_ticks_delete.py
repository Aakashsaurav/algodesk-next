"""Tests for tick deletion safeguards and options."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from typer.testing import CliRunner

from algodesk.api.market import InstrumentId, Tick
from algodesk.cli.app import app
from algodesk.data.catalog import plan_delete
from algodesk.data.manifest import DataManifest, DatasetRecord, FileRecord
from algodesk.data.schema import events_to_table


def _seed_ticks_file(data_dir: Path, inst: str, date_str: str) -> Path:
    """Create a dummy ticks.parquet file and register in manifest."""
    t_dir = (
        data_dir
        / "ticks"
        / "exchange=NSE"
        / "segment=EQ"
        / f"symbol={inst.split(':')[-1]}"
        / f"date={date_str}"
    )
    t_dir.mkdir(parents=True, exist_ok=True)
    f_path = t_dir / "ticks.parquet"

    t = Tick(
        instrument=InstrumentId(inst),
        exch_ts=datetime(2026, 10, 20, 9, 15, tzinfo=UTC),
        recv_ts=datetime(2026, 10, 20, 9, 15, tzinfo=UTC),
        ltp=2500.0,
        ltq=10,
        volume=100,
    )
    tbl = events_to_table([t])
    pq.write_table(tbl, f_path, compression="zstd")

    manifest_db = data_dir / "state" / "data_manifest.db"
    manifest_db.parent.mkdir(parents=True, exist_ok=True)
    m = DataManifest(manifest_db)
    ds_id = f"ticks:{inst}"
    m.record_dataset(
        DatasetRecord(
            dataset_id=ds_id,
            instrument=inst,
            kind="ticks",
            timeframe="tick",
            adjustment="raw",
            source="live",
        )
    )
    rec = FileRecord(
        path=str(f_path),
        instrument=inst,
        kind="ticks",
        timeframe="tick",
        date=date_str,
        rows=1,
        bytes=f_path.stat().st_size,
        first_ts=t.exch_ts.isoformat(),
        last_ts=t.exch_ts.isoformat(),
        sha256="abc",
        source="live",
        state="tick",
        created_ts=datetime.now(UTC).isoformat(),
    )
    m.record_file(rec)
    m.close()
    return f_path


def test_delete_ticks_purge_rejected(tmp_project: Any) -> None:
    _seed_ticks_file(tmp_project / "data", "NSE:EQ:RELIANCE", "2026-10-20")
    runner = CliRunner()
    res = runner.invoke(
        app, ["data", "delete", "NSE:EQ:RELIANCE", "--kind", "ticks", "--purge", "--reason", "test"]
    )
    assert res.exit_code != 0
    assert "Ticks are irreplaceable and cannot be permanently purged" in res.output


def test_delete_ticks_requires_typed_confirmation_even_with_yes(tmp_project: Any) -> None:
    f_path = _seed_ticks_file(tmp_project / "data", "NSE:EQ:RELIANCE", "2026-10-20")
    assert f_path.is_file()

    runner = CliRunner()
    res = runner.invoke(
        app,
        ["data", "delete", "NSE:EQ:RELIANCE", "--kind", "ticks", "-y", "--reason", "test"],
        input="NO\n",
    )
    assert res.exit_code != 0
    assert "Deletion aborted" in res.output
    assert f_path.is_file()


def test_delete_ticks_success_with_confirmation(tmp_project: Any) -> None:
    f_path = _seed_ticks_file(tmp_project / "data", "NSE:EQ:RELIANCE", "2026-10-20")
    runner = CliRunner()

    res = runner.invoke(
        app,
        ["data", "delete", "NSE:EQ:RELIANCE", "--kind", "ticks", "--reason", "Test archival"],
        input="DELETE 1 files\n",
    )
    assert res.exit_code == 0
    assert "Successfully deleted 1 file(s) (moved to safety trash)" in res.output
    assert not f_path.is_file()

    trash_dir = tmp_project / "data" / ".trash"
    assert trash_dir.is_dir()
    found_in_trash = list(trash_dir.glob("**/ticks.parquet"))
    assert len(found_in_trash) == 1


def test_delete_older_than_cutoff(tmp_project: Any) -> None:
    data_dir = tmp_project / "data"
    old_date = (date.today() - timedelta(days=40)).isoformat()
    new_date = (date.today() - timedelta(days=5)).isoformat()
    _seed_ticks_file(data_dir, "NSE:EQ:INFY", old_date)
    _seed_ticks_file(data_dir, "NSE:EQ:INFY", new_date)

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "data",
            "delete",
            "NSE:EQ:INFY",
            "--kind",
            "ticks",
            "--older-than",
            "30",
            "--dry-run",
            "--reason",
            "audit",
        ],
    )
    assert res.exit_code == 0, f"Exception: {res.exception!r}, Output: {res.output}"
    assert old_date in res.output
    assert new_date not in res.output


def test_delete_never_touches_journal_or_config(tmp_project: Any) -> None:
    data_dir = tmp_project / "data"
    _seed_ticks_file(data_dir, "NSE:EQ:RELIANCE", "2026-10-20")
    manifest_db = data_dir / "state" / "data_manifest.db"
    m = DataManifest(manifest_db)

    config_file = tmp_project / "config" / "params" / "data.toml"
    assert config_file.is_file()

    plan = plan_delete(["NSE:EQ:RELIANCE"], m)
    for f in plan.files:
        assert not f.path.endswith(".toml")
        assert not f.path.endswith(".db")
    m.close()
