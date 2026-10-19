"""Tests for ops/checks.py — core platform doctor checks (read-only diagnostics)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from algodesk.api.checks import Severity
from algodesk.journal.writer import Journal, JournalSettings
from algodesk.ops.checks import (
    _check_disk_free,
    _check_env_isolated,
    _check_journal_closed_cleanly,
    _check_journal_integrity,
    _check_params_db,
    _check_paths_ready,
    _check_python_version,
    build_core_checks,
)
from algodesk.ops.runtime import open_runtime


def test_build_core_checks_ids(tmp_path: Path) -> None:
    """build_core_checks registers all expected core diagnostic checks."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "algodesk"\n')
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "platform.toml").write_text('project = "algodesk"\n')

    with open_runtime(tmp_path, ensure=False) as rt:
        checks = build_core_checks(rt)
        check_ids = {c.id for c in checks}
        expected = {
            "python.version",
            "env.isolated",
            "config.valid",
            "paths.ready",
            "params.db",
            "plugins.manifests_valid",
            "plugins.no_order_capable_outside_live",
            "disk.free",
            "journal.integrity",
            "journal.closed_cleanly",
        }
        assert expected.issubset(check_ids), f"Missing core checks: {expected - check_ids}"


def test_checks_are_strictly_read_only(tmp_path: Path) -> None:
    """Executing all core checks creates no new files in the workspace."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "algodesk"\n')
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "platform.toml").write_text('project = "algodesk"\n')

    files_before = set(tmp_path.rglob("*"))
    with open_runtime(tmp_path, ensure=False) as rt:
        checks = build_core_checks(rt)
        for c in checks:
            c.run()
    files_after = set(tmp_path.rglob("*"))
    assert files_before == files_after, "Core checks must be read-only and create no files"


def test_check_python_version_pass_and_fail(tmp_path: Path) -> None:
    """python.version passes for 3.13 and fails when requires-python is unmet."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "algodesk"\nrequires-python = ">=3.13,<3.14"\n'
    )
    res_pass = _check_python_version(tmp_path)
    assert res_pass.ok is True

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "algodesk"\nrequires-python = ">=3.15"\n'
    )
    res_fail = _check_python_version(tmp_path)
    assert res_fail.ok is False
    assert res_fail.fix is not None


def test_check_env_isolated_pass_and_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """env.isolated validates interpreter location and PYTHONNOUSERSITE."""
    from algodesk.ops.paths import find_project_root

    real_root = find_project_root()
    # Baseline isolation in project should pass
    res_ok = _check_env_isolated(real_root)
    assert res_ok.ok is True

    monkeypatch.setenv("PYTHONNOUSERSITE", "0")
    res_fail = _check_env_isolated(real_root)
    assert res_fail.ok is False
    assert "PYTHONNOUSERSITE" in res_fail.message

    monkeypatch.setenv("PYTHONNOUSERSITE", "1")
    monkeypatch.setenv("UV_CACHE_DIR", "/outside/workspace/cache")
    res_fail2 = _check_env_isolated(real_root)
    assert res_fail2.ok is False
    assert "UV_CACHE_DIR points outside project" in res_fail2.message


def test_check_paths_ready_states(tmp_path: Path) -> None:
    """paths.ready detects missing directories as WARNING and unwritable as CRITICAL."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "algodesk"\n')
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "platform.toml").write_text('project = "algodesk"\n')

    # Without ensure, directories are missing -> WARNING
    with open_runtime(tmp_path, ensure=False) as rt:
        res = _check_paths_ready(rt)
        assert res.ok is False
        assert getattr(res, "severity", None) == Severity.WARNING
        assert "Directories not created yet" in res.message

    # With ensure, directories are created -> OK
    with open_runtime(tmp_path, ensure=True) as rt_ensured:
        res_ok = _check_paths_ready(rt_ensured)
        assert res_ok.ok is True

    # Make data dir unwritable -> CRITICAL
    data_dir = tmp_path / "data"
    os.chmod(data_dir, 0o400)
    try:
        with open_runtime(tmp_path, ensure=False) as rt_ro:
            res_ro = _check_paths_ready(rt_ro)
            assert res_ro.ok is False
            assert getattr(res_ro, "severity", None) == Severity.CRITICAL
            assert "not writable" in res_ro.message
    finally:
        os.chmod(data_dir, 0o700)


def test_check_params_db_states(tmp_path: Path) -> None:
    """params.db passes when absent, passes when current, and fails on corruption or schema."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "algodesk"\n')
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "platform.toml").write_text('project = "algodesk"\n')

    # Absent -> OK
    with open_runtime(tmp_path, ensure=False) as rt:
        assert _check_params_db(rt).ok is True

    # Initialized -> OK
    with open_runtime(tmp_path, ensure=True) as rt:
        assert _check_params_db(rt).ok is True

        # Simulate newer schema version -> FAIL
        conn = sqlite3.connect(rt.state.params_db)
        conn.execute("UPDATE schema_version SET version = 999;")
        conn.commit()
        conn.close()

        res_newer = _check_params_db(rt)
        assert res_newer.ok is False
        assert "newer than supported" in res_newer.message

        # Corrupt DB file -> FAIL
        rt.state.params_db.write_bytes(b"corrupted binary data")
        res_corrupt = _check_params_db(rt)
        assert res_corrupt.ok is False


def test_check_disk_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """disk.free warns when available space is below requirement."""
    import collections

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "algodesk"\n')
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "platform.toml").write_text('project = "algodesk"\n')

    DiskUsage = collections.namedtuple("DiskUsage", ["total", "used", "free"])

    with open_runtime(tmp_path, ensure=False) as rt:
        # Mock disk usage with ample space (100 GB)
        monkeypatch.setattr(
            "shutil.disk_usage",
            lambda p: DiskUsage(200 * 1024**3, 100 * 1024**3, 100 * 1024**3),
        )
        res_ok = _check_disk_free(rt)
        assert res_ok.ok is True

        # Mock low disk space (1 GB free)
        monkeypatch.setattr(
            "shutil.disk_usage",
            lambda p: DiskUsage(200 * 1024**3, 199 * 1024**3, 1 * 1024**3),
        )
        res_low = _check_disk_free(rt)
        assert res_low.ok is False
        assert "below minimum" in res_low.message


def test_check_journal_integrity_and_clean_runs(tmp_path: Path) -> None:
    """journal.integrity and journal.closed_cleanly test absent, clean, and broken states."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "algodesk"\n')
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "platform.toml").write_text('project = "algodesk"\n')

    with open_runtime(tmp_path, ensure=True) as rt:
        # Absent journal -> pass
        assert _check_journal_integrity(rt).ok is True
        assert _check_journal_closed_cleanly(rt).ok is True

        # Write clean run
        settings = JournalSettings(
            flush_interval_ms=10, max_buffer_events=100, backpressure_timeout_ms=1000
        )
        with Journal(rt.state.journal_db, settings=settings) as j:
            j.start_run("paper", "v1", "c1", "r1")
            j.append("test.event", {"k": "v"})
            j.end_run("ok")

        assert _check_journal_integrity(rt).ok is True
        assert _check_journal_closed_cleanly(rt).ok is True

        # Start an unclosed run -> warning
        with Journal(rt.state.journal_db, settings=settings) as j2:
            j2.start_run("paper", "v1", "c1", "r2")
            j2.append("test.event2", {"k": "v2"})

        res_unclosed = _check_journal_closed_cleanly(rt)
        assert res_unclosed.ok is False
        assert "unclosed run" in res_unclosed.message

        # Tamper with journal row -> integrity failure
        conn = sqlite3.connect(rt.state.journal_db)
        conn.execute("DROP TRIGGER trg_events_no_update;")
        conn.execute("UPDATE events SET payload_json = '{\"tampered\": true}' WHERE seq = 1;")
        conn.commit()
        conn.close()

        res_tampered = _check_journal_integrity(rt)
        assert res_tampered.ok is False
        assert "integrity check failed" in res_tampered.message
