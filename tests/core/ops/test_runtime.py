"""Tests for ops/runtime.py — composition root and runtime wiring."""

from __future__ import annotations

from pathlib import Path

from algodesk.api.kinds import Mode
from algodesk.ops.runtime import open_runtime


def test_open_runtime_default_workspace() -> None:
    """open_runtime on current workspace discovers root and builds effective config."""
    with open_runtime() as rt:
        assert (rt.root / "pyproject.toml").is_file()
        assert rt.effective is not None
        assert rt.paths is not None
        assert rt.state is not None
        assert rt.plugins is not None


def test_open_runtime_ensure_false_fresh_root(tmp_path: Path) -> None:
    """ensure=False creates no directories and params is None when db absent."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "algodesk"\n')
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "platform.toml").write_text('project = "algodesk"\n')

    rt = open_runtime(tmp_path, ensure=False)
    try:
        assert not rt.paths.data.exists()
        assert not rt.paths.logs.exists()
        assert rt.params is None
    finally:
        rt.close()


def test_open_runtime_ensure_true_creates_dirs_and_params(tmp_path: Path) -> None:
    """ensure=True creates directories and opens params registry with loaded defaults."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "algodesk"\n')
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "platform.toml").write_text('project = "algodesk"\n')

    params_dir = config_dir / "params"
    params_dir.mkdir()
    (params_dir / "core.toml").write_text(
        '[[param]]\nname = "test.param"\ntype = "int"\ndefault = 42\n'
    )

    with open_runtime(tmp_path, ensure=True) as rt:
        assert rt.paths.data.is_dir()
        assert rt.paths.logs.is_dir()
        assert rt.paths.results.is_dir()
        assert rt.params is not None
        assert rt.params.get("test.param") == 42


def test_open_runtime_existing_params_db_loaded_without_ensure(tmp_path: Path) -> None:
    """When params.db exists, open_runtime loads params even with ensure=False."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "algodesk"\n')
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "platform.toml").write_text('project = "algodesk"\n')

    params_dir = config_dir / "params"
    params_dir.mkdir()
    (params_dir / "core.toml").write_text(
        '[[param]]\nname = "test.existing"\ntype = "str"\ndefault = "active"\n'
    )

    # First open with ensure=True to create database
    with open_runtime(tmp_path, ensure=True) as rt:
        assert rt.params is not None

    # Now open with ensure=False — should open existing db
    with open_runtime(tmp_path, ensure=False) as rt2:
        assert rt2.params is not None
        assert rt2.params.get("test.existing") == "active"


def test_open_runtime_custom_mode(tmp_path: Path) -> None:
    """open_runtime passes mode to ParamRegistry."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "algodesk"\n')
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "platform.toml").write_text('project = "algodesk"\n')

    with open_runtime(tmp_path, mode=Mode.PAPER, ensure=True) as rt:
        assert rt.params is not None
        assert rt.params._mode == Mode.PAPER
