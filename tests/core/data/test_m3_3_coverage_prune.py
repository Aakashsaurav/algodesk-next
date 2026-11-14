"""Unit tests for 'algodesk data coverage prune' and pollution cleanup."""

from __future__ import annotations

import json
from typing import Any

from typer.testing import CliRunner

from algodesk.api.market import CoverageStatus
from algodesk.cli.app import app
from algodesk.data.coverage import prune_unmappable_coverage
from algodesk.data.manifest import CoverageDayRecord, DataManifest


def _seed_test_pollution(manifest: DataManifest) -> None:
    conn = manifest._conn
    # 1. Valid dataset with parts
    insert_ds = (
        "INSERT OR REPLACE INTO datasets "
        "(dataset_id, instrument, kind, timeframe, adjustment, source, schema_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, datetime('now'))"
    )
    conn.execute(
        insert_ds,
        ("bars:upstox:1d:NSE:EQ:RELIANCE:raw", "NSE:EQ:RELIANCE", "bars", "1d", "raw", "upstox"),
    )
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id="bars:upstox:1d:NSE:EQ:RELIANCE:raw",
            day="2026-11-04",
            status=CoverageStatus.COMPLETE.value,
            rows=1,
            expected_rows=1,
            part_path="bars/upstox/1d/sample.parquet",
            fetched_at="2026-11-04T10:00:00Z",
        )
    )
    # Register part for valid dataset
    conn.execute(
        "INSERT INTO parts (path, dataset_id, rows, bytes, first_ts, last_ts, sha256, "
        "source, source_version, fetched_at) "
        "VALUES ('bars/upstox/1d/sample.parquet', 'bars:upstox:1d:NSE:EQ:RELIANCE:raw', "
        "1, 100, '2026-11-04', '2026-11-04', 'sha', 'upstox', '1', 'now')"
    )

    # 2. Pollution dataset: unmappable symbol, 0 parts, 0 files
    conn.execute(
        insert_ds,
        (
            "bars:upstox:1d:NSE:EQ:NONEXISTENT_SYMBOL:adjusted",
            "NSE:EQ:NONEXISTENT_SYMBOL",
            "bars",
            "1d",
            "adjusted",
            "upstox",
        ),
    )
    for d_str in ("2026-01-01", "2026-01-02", "2026-01-05"):
        manifest.record_coverage_day(
            CoverageDayRecord(
                dataset_id="bars:upstox:1d:NSE:EQ:NONEXISTENT_SYMBOL:adjusted",
                day=d_str,
                status=CoverageStatus.EMPTY.value,
                rows=0,
                expected_rows=0,
                part_path="",
                fetched_at="2026-11-13T05:20:12Z",
                note="Cannot map unknown symbol in Upstox instrument master",
            )
        )
    # Insert fetch_log entry for pollution
    conn.execute(
        "INSERT INTO fetch_log (chunk, source, started, finished, rows, outcome, error) "
        "VALUES ('NSE:EQ:NONEXISTENT_SYMBOL:upstox:1d:2026-01-01_2026-01-05', 'upstox', "
        "'2026-11-13', '2026-11-13', 0, 'FAILED', 'Cannot map symbol')"
    )
    conn.commit()


def test_prune_unmappable_coverage_dry_run(tmp_project: Any) -> None:
    manifest_p = tmp_project / "data" / "state" / "data_manifest.db"
    manifest_p.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(manifest_p)

    _seed_test_pollution(manifest)

    report = prune_unmappable_coverage(manifest, dry_run=True)
    assert report.dry_run is True
    assert "bars:upstox:1d:NSE:EQ:NONEXISTENT_SYMBOL:adjusted" in report.pruned_datasets
    assert report.deleted_coverage_rows == 3
    assert report.deleted_fetch_log_rows == 1

    # Verify rows were NOT deleted
    cursor = manifest._conn.cursor()
    cursor.execute(
        "SELECT count(*) FROM coverage_days "
        "WHERE dataset_id = 'bars:upstox:1d:NSE:EQ:NONEXISTENT_SYMBOL:adjusted'"
    )
    assert cursor.fetchone()[0] == 3
    manifest.close()


def test_prune_unmappable_coverage_deletes_orphan_records(tmp_project: Any) -> None:
    manifest_p = tmp_project / "data" / "state" / "data_manifest.db"
    manifest_p.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(manifest_p)

    _seed_test_pollution(manifest)

    report = prune_unmappable_coverage(manifest, dry_run=False)
    assert report.dry_run is False
    assert report.deleted_coverage_rows == 3

    # Verify rows WERE deleted
    cursor = manifest._conn.cursor()
    cursor.execute(
        "SELECT count(*) FROM coverage_days "
        "WHERE dataset_id = 'bars:upstox:1d:NSE:EQ:NONEXISTENT_SYMBOL:adjusted'"
    )
    assert cursor.fetchone()[0] == 0
    cursor.execute(
        "SELECT count(*) FROM datasets "
        "WHERE dataset_id = 'bars:upstox:1d:NSE:EQ:NONEXISTENT_SYMBOL:adjusted'"
    )
    assert cursor.fetchone()[0] == 0

    # Verify valid dataset was preserved
    cursor.execute(
        "SELECT count(*) FROM datasets WHERE dataset_id = 'bars:upstox:1d:NSE:EQ:RELIANCE:raw'"
    )
    assert cursor.fetchone()[0] == 1
    manifest.close()


def test_cli_data_coverage_prune(tmp_project: Any) -> None:
    manifest_p = tmp_project / "data" / "state" / "data_manifest.db"
    manifest_p.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(manifest_p)
    _seed_test_pollution(manifest)
    manifest.close()

    runner = CliRunner()
    # Dry run
    res_dry = runner.invoke(app, ["data", "coverage", "prune", "--dry-run"])
    assert res_dry.exit_code == 0
    assert "identified for deletion (dry run)" in res_dry.output
    assert "bars:upstox:1d:NSE:EQ:NONEXISTENT_SYMBOL:adjusted" in res_dry.output

    # Actual prune
    res_prune = runner.invoke(app, ["data", "coverage", "prune"])
    assert res_prune.exit_code == 0
    assert "deleted:" in res_prune.output

    # Subsequent run finds no pollution
    res_clean = runner.invoke(app, ["data", "coverage", "prune"])
    assert res_clean.exit_code == 0
    assert "No unmappable coverage pollution found." in res_clean.output


def test_cli_data_coverage_prune_json(tmp_project: Any) -> None:
    manifest_p = tmp_project / "data" / "state" / "data_manifest.db"
    manifest_p.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(manifest_p)
    _seed_test_pollution(manifest)
    manifest.close()

    runner = CliRunner()
    res = runner.invoke(app, ["data", "coverage", "prune", "--dry-run", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["dry_run"] is True
    assert "bars:upstox:1d:NSE:EQ:NONEXISTENT_SYMBOL:adjusted" in data["pruned_datasets"]
    assert data["deleted_coverage_rows"] == 3


def test_data_verify_fix_invokes_prune(tmp_project: Any) -> None:
    manifest_p = tmp_project / "data" / "state" / "data_manifest.db"
    manifest_p.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(manifest_p)
    conn = manifest._conn
    insert_ds = (
        "INSERT OR REPLACE INTO datasets "
        "(dataset_id, instrument, kind, timeframe, adjustment, source, schema_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, datetime('now'))"
    )
    conn.execute(
        insert_ds,
        (
            "bars:upstox:1d:NSE:EQ:NONEXISTENT_SYMBOL:adjusted",
            "NSE:EQ:NONEXISTENT_SYMBOL",
            "bars",
            "1d",
            "adjusted",
            "upstox",
        ),
    )
    for d_str in ("2026-01-01", "2026-01-02", "2026-01-05"):
        manifest.record_coverage_day(
            CoverageDayRecord(
                dataset_id="bars:upstox:1d:NSE:EQ:NONEXISTENT_SYMBOL:adjusted",
                day=d_str,
                status=CoverageStatus.EMPTY.value,
                rows=0,
                expected_rows=0,
                part_path="",
                fetched_at="2026-11-13T05:20:12Z",
                note="Cannot map unknown symbol in Upstox instrument master",
            )
        )
    conn.commit()
    manifest.close()

    runner = CliRunner()
    res = runner.invoke(app, ["data", "verify", "--fix"])
    assert res.exit_code == 0
    assert "Pruned 1 unmappable dataset(s)" in res.output


def test_prune_unmappable_coverage_with_no_pollution(tmp_project: Any) -> None:
    manifest_p = tmp_project / "data" / "state" / "data_manifest.db"
    manifest_p.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(manifest_p)
    rep = prune_unmappable_coverage(manifest, dry_run=False)
    assert len(rep.pruned_datasets) == 0
    assert rep.deleted_coverage_rows == 0
    assert rep.deleted_fetch_log_rows == 0


def test_prune_unmappable_coverage_keeps_dataset_with_files(tmp_project: Any) -> None:
    manifest_p = tmp_project / "data" / "state" / "data_manifest.db"
    manifest_p.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(manifest_p)
    conn = manifest._conn

    # Register an unmappable symbol BUT it has a file in 'files' table
    insert_ds = (
        "INSERT OR REPLACE INTO datasets "
        "(dataset_id, instrument, kind, timeframe, adjustment, source, schema_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, datetime('now'))"
    )
    conn.execute(
        insert_ds,
        (
            "bars:upstox:1d:NSE:EQ:SPECIAL_SYM:raw",
            "NSE:EQ:SPECIAL_SYM",
            "bars",
            "1d",
            "raw",
            "upstox",
        ),
    )
    insert_files = (
        "INSERT INTO files "
        "(path, instrument, kind, timeframe, date, rows, bytes, first_ts, last_ts, "
        "sha256, source, state, created_ts) "
        "VALUES ('bars/upstox/1d/special.parquet', 'NSE:EQ:SPECIAL_SYM', 'bars', '1d', "
        "'2026-11-04', 1, 100, '2026-11-04', '2026-11-04', 'sha', 'upstox', 'COMMITTED', 'now')"
    )
    conn.execute(insert_files)
    conn.commit()

    rep = prune_unmappable_coverage(manifest, dry_run=False)
    # Must NOT prune because it has stored files!
    assert "bars:upstox:1d:NSE:EQ:SPECIAL_SYM:raw" not in rep.pruned_datasets
