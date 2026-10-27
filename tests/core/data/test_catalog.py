"""Tests for data catalog coverage, selector resolution, and safe deletion."""

from __future__ import annotations

import fcntl
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from algodesk.api.errors import ConfigError, DataError
from algodesk.data.catalog import (
    coverage,
    execute_delete,
    list_trash,
    plan_delete,
    purge_trash,
    resolve_selector,
)
from algodesk.data.manifest import DataManifest, FileRecord, InstrumentRecord


@pytest.fixture
def manifest_and_data(tmp_path: Path) -> tuple[DataManifest, Path, Path]:
    db_path = tmp_path / "state" / "data_manifest.db"
    manifest = DataManifest(db_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    groups_dir = tmp_path / "groups"
    groups_dir.mkdir(parents=True, exist_ok=True)

    # Populate instruments
    manifest.record_instruments(
        [
            InstrumentRecord(
                id="NSE:EQ:HDFCBANK",
                exchange="NSE",
                segment="EQ",
                trading_symbol="HDFCBANK",
                kind="equity",
                underlying="HDFCBANK",
            ),
            InstrumentRecord(
                id="BSE:EQ:HDFCBANK",
                exchange="BSE",
                segment="EQ",
                trading_symbol="HDFCBANK",
                kind="equity",
                underlying="HDFCBANK",
            ),
            InstrumentRecord(
                id="NSE:EQ:SBIN",
                exchange="NSE",
                segment="EQ",
                trading_symbol="SBIN",
                kind="equity",
                underlying="SBIN",
            ),
            InstrumentRecord(
                id="NSE:FO:SBINOPT310031MARCH26",
                exchange="NSE",
                segment="FO",
                trading_symbol="SBINOPT310031March26",
                kind="derivative",
                underlying="SBIN",
            ),
        ]
    )

    # Create dummy files
    p1 = data_dir / "ticks" / "f1.parquet"
    p1.parent.mkdir(parents=True, exist_ok=True)
    p1.write_bytes(b"dummy1")

    p2 = data_dir / "ticks" / "f2.parquet"
    p2.write_bytes(b"dummy2")

    manifest.record_file(
        FileRecord(
            path=str(p1),
            instrument="NSE:EQ:HDFCBANK",
            kind="ticks",
            timeframe="tick",
            date="2026-10-25",
            rows=100,
            bytes=len(b"dummy1"),
            first_ts="2026-10-25T09:15:00+00:00",
            last_ts="2026-10-25T15:30:00+00:00",
            sha256="abc1",
            source="test",
            state="day",
            created_ts=datetime.now(UTC).isoformat(),
        )
    )
    manifest.record_file(
        FileRecord(
            path=str(p2),
            instrument="NSE:EQ:SBIN",
            kind="ticks",
            timeframe="tick",
            date="2026-10-25",
            rows=200,
            bytes=len(b"dummy2"),
            first_ts="2026-10-25T09:15:00+00:00",
            last_ts="2026-10-25T15:30:00+00:00",
            sha256="abc2",
            source="test",
            state="day",
            created_ts=datetime.now(UTC).isoformat(),
        )
    )

    # Group file
    (groups_dir / "banks.toml").write_text(
        'name = "banks"\nmembers = ["NSE:EQ:HDFCBANK", "NSE:EQ:SBIN"]\n',
        encoding="utf-8",
    )

    return manifest, data_dir, groups_dir


def test_resolve_exact_id(manifest_and_data: tuple[DataManifest, Path, Path]) -> None:
    manifest, _, groups_dir = manifest_and_data
    res = resolve_selector("NSE:EQ:SBIN", manifest, groups_dir)
    assert res == ["NSE:EQ:SBIN"]


def test_resolve_group(manifest_and_data: tuple[DataManifest, Path, Path]) -> None:
    manifest, _, groups_dir = manifest_and_data
    res = resolve_selector("@banks", manifest, groups_dir)
    assert sorted(res) == ["NSE:EQ:HDFCBANK", "NSE:EQ:SBIN"]


def test_resolve_unknown_group_fails_closed(
    manifest_and_data: tuple[DataManifest, Path, Path],
) -> None:
    manifest, _, groups_dir = manifest_and_data
    with pytest.raises(DataError) as exc_info:
        resolve_selector("@unknown_group", manifest, groups_dir)
    assert "Unknown group selector" in exc_info.value.what


def test_resolve_ambiguous_bare_symbol(manifest_and_data: tuple[DataManifest, Path, Path]) -> None:
    manifest, _, groups_dir = manifest_and_data
    with pytest.raises(DataError) as exc_info:
        resolve_selector("HDFCBANK", manifest, groups_dir)
    assert "Ambiguous" in exc_info.value.what
    assert "Specify full canonical ID" in exc_info.value.fix


def test_resolve_unique_bare_symbol(manifest_and_data: tuple[DataManifest, Path, Path]) -> None:
    manifest, _, groups_dir = manifest_and_data
    res = resolve_selector("SBIN", manifest, groups_dir)
    assert res == ["NSE:EQ:SBIN"]


def test_resolve_glob(manifest_and_data: tuple[DataManifest, Path, Path]) -> None:
    manifest, _, groups_dir = manifest_and_data
    res = resolve_selector("NSE:EQ:*", manifest, groups_dir)
    assert sorted(res) == ["NSE:EQ:HDFCBANK", "NSE:EQ:SBIN"]


def test_resolve_compact_contract_name(manifest_and_data: tuple[DataManifest, Path, Path]) -> None:
    manifest, _, groups_dir = manifest_and_data
    # Space and punctuation ignored
    res = resolve_selector("sbin-opt-3100-31march26", manifest, groups_dir)
    assert res == ["NSE:FO:SBINOPT310031MARCH26"]


def test_resolve_unknown_selector_fails_closed(
    manifest_and_data: tuple[DataManifest, Path, Path],
) -> None:
    manifest, _, groups_dir = manifest_and_data
    with pytest.raises(DataError) as exc_info:
        resolve_selector("NONEXISTENT_XYZ", manifest, groups_dir)
    assert "Unknown selector" in exc_info.value.what


def test_coverage_aggregation(manifest_and_data: tuple[DataManifest, Path, Path]) -> None:
    manifest, _, groups_dir = manifest_and_data
    rows = coverage(manifest, group_by="exchange", groups_dir=groups_dir)
    assert len(rows) == 1
    assert rows[0].target == "NSE"
    assert rows[0].rows == 300
    assert rows[0].days == 1


def test_coverage_group_by_group(manifest_and_data: tuple[DataManifest, Path, Path]) -> None:
    manifest, _, groups_dir = manifest_and_data
    rows = coverage(manifest, group_by="group", groups_dir=groups_dir)
    assert len(rows) == 1
    assert rows[0].target == "banks"
    assert rows[0].rows == 300


def test_plan_and_execute_delete_to_trash(
    manifest_and_data: tuple[DataManifest, Path, Path],
) -> None:
    manifest, data_dir, groups_dir = manifest_and_data
    plan = plan_delete(["NSE:EQ:SBIN"], manifest, groups_dir=groups_dir)
    assert plan.total_files == 1
    assert plan.total_rows == 200

    deleted_count = execute_delete(
        plan, manifest, data_dir=data_dir, purge=False, reason="test delete"
    )
    assert deleted_count == 1
    assert len(manifest.get_files(instrument="NSE:EQ:SBIN")) == 0

    # Verify moved to trash
    trash = list_trash(data_dir)
    assert len(trash) == 1
    assert trash[0].files_count == 1
    assert trash[0].reason == "test delete"

    # Purge trash
    purged = purge_trash(data_dir)
    assert purged == 1
    assert len(list_trash(data_dir)) == 0


def test_execute_delete_purge_immediate(manifest_and_data: tuple[DataManifest, Path, Path]) -> None:
    manifest, data_dir, groups_dir = manifest_and_data
    plan = plan_delete(["NSE:EQ:HDFCBANK"], manifest, groups_dir=groups_dir)
    file_path = Path(plan.files[0].path)
    assert file_path.is_file()

    deleted_count = execute_delete(
        plan, manifest, data_dir=data_dir, purge=True, reason="immediate purge"
    )
    assert deleted_count == 1
    assert not file_path.is_file()
    assert len(list_trash(data_dir)) == 0


def test_delete_refuses_when_journal_locked(
    manifest_and_data: tuple[DataManifest, Path, Path],
    tmp_path: Path,
) -> None:
    if sys.platform == "win32":
        pytest.skip("fcntl lock test on POSIX")
    manifest, data_dir, groups_dir = manifest_and_data
    plan = plan_delete(["NSE:EQ:HDFCBANK"], manifest, groups_dir=groups_dir)

    lock_file = tmp_path / "journal.db.lock"
    lock_file.write_text("locked", encoding="utf-8")
    fd = open(lock_file, "a")
    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    try:
        with pytest.raises(ConfigError) as exc_info:
            execute_delete(plan, manifest, data_dir=data_dir, lock_path=lock_file)
        assert "Refusing deletion while journal writer lock is held" in exc_info.value.what
    finally:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()
