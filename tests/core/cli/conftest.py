"""Fixtures and test helpers for CLI test suite."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    """Provide a fresh Typer CliRunner."""
    return CliRunner()


@pytest.fixture
def make_project(tmp_path: Path) -> Callable[[str], Path]:
    """Factory fixture to build a valid isolated AlgoDesk workspace root."""

    def _factory(name: str = "proj") -> Path:
        p = tmp_path / name
        p.mkdir(parents=True, exist_ok=True)
        (p / "pyproject.toml").write_text(
            '[project]\nname = "algodesk"\nversion = "0.0.0"\nrequires-python = ">=3.13,<3.14"\n'
        )
        cfg_dir = p / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "platform.toml").write_text(
            'project = "algodesk"\n[paths]\ndata_dir = "data"\n'
            'logs_dir = "logs"\nresults_dir = "results"\nlocal_dir = ".local"\n'
        )
        params_dir = cfg_dir / "params"
        params_dir.mkdir(parents=True, exist_ok=True)
        (params_dir / "core.toml").write_text(
            '[[param]]\nname = "ops.min_free_disk_gb"\ntype = "int"\ndefault = 5\n\n'
            '[[param]]\nname = "risk.conservative.daily_loss_limit_pct"\n'
            'type = "float"\ndefault = 1.5\ntighten = "down"\n'
        )
        plugins_dir = p / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        return p

    return _factory
