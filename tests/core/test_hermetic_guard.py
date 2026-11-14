"""Tests for hermetic test session guard and state change detection."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from _pytest.outcomes import Exit

from tests.conftest import (
    pytest_sessionfinish,
    snapshot_guarded_hashes,
    snapshot_guarded_paths,
    verify_guarded_hashes,
    verify_repo_state_snapshot,
)


def test_snapshot_guarded_paths_captures_structure(tmp_path: Path) -> None:
    """snapshot_guarded_paths captures relative paths in data/, results/, and .algodesk/."""
    data_dir = tmp_path / "data" / "ticks"
    data_dir.mkdir(parents=True)
    (data_dir / "sample.parquet").write_bytes(b"DATA")

    results_dir = tmp_path / "results" / "run_1"
    results_dir.mkdir(parents=True)
    (results_dir / "metrics.json").write_text("{}")

    state_dir = tmp_path / ".algodesk"
    state_dir.mkdir(parents=True)
    (state_dir / "conformance.json").write_text("{}")

    # Ignored files
    (data_dir / ".DS_Store").write_text("junk")
    (results_dir / "test.pyc").write_bytes(b"bytecode")

    snapshot = snapshot_guarded_paths(tmp_path)
    assert "data/ticks/sample.parquet" in snapshot
    assert "results/run_1/metrics.json" in snapshot
    assert ".algodesk/conformance.json" in snapshot
    assert "data/ticks/.DS_Store" not in snapshot
    assert "results/run_1/test.pyc" not in snapshot


def test_verify_repo_state_snapshot_passes_on_identical() -> None:
    """verify_repo_state_snapshot does not raise when before and after match."""
    state = {"data/a.parquet", "results/b.json", ".algodesk/c.json"}
    verify_repo_state_snapshot(state, state)


def test_verify_repo_state_snapshot_detects_additions() -> None:
    """verify_repo_state_snapshot raises RuntimeError with added file diff."""
    before = {"data/a.parquet"}
    after = {"data/a.parquet", "data/leaked.parquet"}
    with pytest.raises(RuntimeError) as exc_info:
        verify_repo_state_snapshot(before, after)
    assert "Hermetic test violation" in str(exc_info.value)
    assert "Added files (1): data/leaked.parquet" in str(exc_info.value)


def test_verify_repo_state_snapshot_detects_deletions() -> None:
    """verify_repo_state_snapshot raises RuntimeError with removed file diff."""
    before = {"data/a.parquet", ".algodesk/key.json"}
    after = {"data/a.parquet"}
    with pytest.raises(RuntimeError) as exc_info:
        verify_repo_state_snapshot(before, after)
    assert "Hermetic test violation" in str(exc_info.value)
    assert "Removed files (1): .algodesk/key.json" in str(exc_info.value)


def test_session_guard_pytest_exit_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    """pytest_sessionfinish invokes pytest.exit(..., returncode=1) when guard triggers."""
    import tests.conftest as conf

    # Set mock before snapshot
    monkeypatch.setattr(conf, "_BEFORE_TEST_SESSION_SNAPSHOT", {"data/original.parquet"})
    # Monkeypatch snapshot_guarded_paths to return an altered after snapshot
    monkeypatch.setattr(
        conf,
        "snapshot_guarded_paths",
        lambda root: {"data/original.parquet", "data/leaked_during_test.parquet"},
    )

    mock_session = MagicMock()
    # Ensure workerinput does not exist (acting as controller)
    del mock_session.config.workerinput

    with pytest.raises(Exit) as exc_info:
        pytest_sessionfinish(mock_session, 0)

    assert "Session Guard Failed" in str(exc_info.value)
    assert "data/leaked_during_test.parquet" in str(exc_info.value)


def test_snapshot_guarded_hashes_captures_db_and_config(tmp_path: Path) -> None:
    """snapshot_guarded_hashes records sha256 for state DBs and group/screen tomls."""
    state_dir = tmp_path / "data" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "data_manifest.db").write_bytes(b"SQLITE_DATA")
    (state_dir / "data_manifest.db-wal").write_bytes(b"WAL")

    groups_dir = tmp_path / "config" / "groups"
    groups_dir.mkdir(parents=True)
    (groups_dir / "demo.toml").write_text('name = "demo"')

    screens_dir = tmp_path / "config" / "screens"
    screens_dir.mkdir(parents=True)
    (screens_dir / "screen.toml").write_text('name = "screen"')

    hashes = snapshot_guarded_hashes(tmp_path)
    assert "data/state/data_manifest.db" in hashes
    assert "data/state/data_manifest.db-wal" not in hashes
    assert "config/groups/demo.toml" in hashes
    assert "config/screens/screen.toml" in hashes


def test_verify_guarded_hashes_detects_manifest_modification(tmp_path: Path) -> None:
    """verify_guarded_hashes raises RuntimeError when a manifest DB is modified in place."""
    state_dir = tmp_path / "data" / "state"
    state_dir.mkdir(parents=True)
    manifest_copy = state_dir / "data_manifest.db"
    manifest_copy.write_bytes(b"INITIAL_DB_CONTENT")

    before = snapshot_guarded_hashes(tmp_path)
    # Mutate the copy of manifest DB in place
    manifest_copy.write_bytes(b"POLLUTED_DB_CONTENT_NONEXISTENT_SYMBOL")
    after = snapshot_guarded_hashes(tmp_path)

    with pytest.raises(RuntimeError) as exc_info:
        verify_guarded_hashes(before, after)

    err = str(exc_info.value)
    assert "Hermetic test violation" in err
    assert "Guarded file modified: data/state/data_manifest.db" in err


def test_session_guard_pytest_exit_fires_on_db_hash_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pytest_sessionfinish exits 1 when a guarded database hash changes."""
    import tests.conftest as conf

    fake_hash1 = "1" * 64
    fake_hash2 = "2" * 64
    monkeypatch.setattr(conf, "_BEFORE_TEST_SESSION_SNAPSHOT", set())
    monkeypatch.setattr(
        conf,
        "_BEFORE_TEST_SESSION_HASHES",
        {"data/state/data_manifest.db": fake_hash1},
    )
    monkeypatch.setattr(conf, "snapshot_guarded_paths", lambda root: set())
    monkeypatch.setattr(
        conf,
        "snapshot_guarded_hashes",
        lambda root: {"data/state/data_manifest.db": fake_hash2},
    )

    mock_session = MagicMock()
    del mock_session.config.workerinput

    with pytest.raises(Exit) as exc_info:
        pytest_sessionfinish(mock_session, 0)

    assert "Session Guard Failed" in str(exc_info.value)
    assert "data/state/data_manifest.db" in str(exc_info.value)
