"""Tests for repository and environment isolation (NFR-14)."""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path


def _get_root() -> Path:
    """Return the repository root directory."""
    env_root = os.environ.get("ROOT")
    if env_root:
        return Path(env_root).resolve()
    # tests/core/test_isolation.py -> parents[2] is the project root
    return Path(__file__).resolve().parents[2]


def test_sys_prefix_under_venv() -> None:
    """sys.prefix must be under ROOT/.venv."""
    root = _get_root()
    venv_dir = (root / ".venv").resolve()
    prefix = Path(sys.prefix).resolve()
    assert prefix == venv_dir or str(prefix).startswith(f"{venv_dir}{os.sep}")


def test_sys_executable_under_local_python() -> None:
    """realpath(sys.executable) must be under ROOT/.local/python."""
    root = _get_root()
    local_python = (root / ".local" / "python").resolve()
    real_exec = Path(os.path.realpath(sys.executable)).resolve()
    assert str(real_exec).startswith(f"{local_python}{os.sep}")


def test_no_user_site_flag() -> None:
    """sys.flags.no_user_site must equal 1."""
    assert sys.flags.no_user_site == 1


def test_uv_python_install_dir_starts_with_root() -> None:
    """UV_PYTHON_INSTALL_DIR env var must start with ROOT."""
    root = _get_root()
    install_dir = os.environ.get("UV_PYTHON_INSTALL_DIR", "")
    assert install_dir.startswith(str(root))


def test_pyproject_uv_isolation_settings() -> None:
    """pyproject.toml [tool.uv] must enforce python-preference and python-downloads."""
    root = _get_root()
    pyproject_path = root / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)
    uv_conf = data.get("tool", {}).get("uv", {})
    assert uv_conf.get("python-preference") == "only-managed"
    assert uv_conf.get("python-downloads") == "manual"
