"""Tests for ops/paths.py — project root discovery and state paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from algodesk.api.errors import ConfigError
from algodesk.ops.paths import StatePaths, find_project_root


def test_find_project_root_from_current() -> None:
    """find_project_root finds the algodesk repository root."""
    root = find_project_root()
    assert (root / "pyproject.toml").is_file()
    assert (root / "src" / "algodesk").is_dir()


def test_find_project_root_from_nested_subdir() -> None:
    """find_project_root walks upward from deep subdirectory."""
    root = find_project_root()
    nested = root / "src" / "algodesk" / "ops"
    discovered = find_project_root(nested)
    assert discovered == root


def test_find_project_root_missing_raises_config_error(tmp_path: Path) -> None:
    """Directory tree with no algodesk pyproject.toml raises ConfigError."""
    sub = tmp_path / "a" / "b" / "c"
    sub.mkdir(parents=True)
    with pytest.raises(ConfigError) as exc_info:
        find_project_root(sub)
    assert "Project root not found" in exc_info.value.what


def test_find_project_root_wrong_project_name(tmp_path: Path) -> None:
    """pyproject.toml with non-algodesk name is ignored and raises ConfigError."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "other_project"\n')
    sub = tmp_path / "child"
    sub.mkdir()
    with pytest.raises(ConfigError) as exc_info:
        find_project_root(sub)
    assert "Project root not found" in exc_info.value.what


def test_find_project_root_malformed_pyproject_ignored(tmp_path: Path) -> None:
    """Malformed pyproject.toml is safely skipped."""
    (tmp_path / "pyproject.toml").write_text("invalid toml content [[[")
    with pytest.raises(ConfigError) as exc_info:
        find_project_root(tmp_path)
    assert "Project root not found" in exc_info.value.what


def test_state_paths_resolve() -> None:
    """StatePaths.resolve builds params.db, journal.db, and conformance_dir paths."""
    root = Path("/workspace/test")
    data_dir = root / "data"
    sp = StatePaths.resolve(root, data_dir)
    assert sp.params_db == data_dir / "state" / "params.db"
    assert sp.journal_db == data_dir / "state" / "journal.db"
    assert sp.conformance_dir == root / ".algodesk"


def test_state_paths_constructor_variants() -> None:
    """StatePaths supports positional and keyword initialization."""
    sp1 = StatePaths(Path("/r"), Path("/d"))
    assert sp1.params_db == Path("/d/state/params.db")
    assert sp1.journal_db == Path("/d/state/journal.db")
    assert sp1.conformance_dir == Path("/r/.algodesk")

    sp2 = StatePaths(
        params_db=Path("/a/p.db"),
        journal_db=Path("/a/j.db"),
        conformance_dir=Path("/a/conf"),
    )
    assert sp2.params_db == Path("/a/p.db")
    assert sp2.journal_db == Path("/a/j.db")
    assert sp2.conformance_dir == Path("/a/conf")
