"""Smoke tests: version and package imports."""

from __future__ import annotations

import importlib


def test_version() -> None:
    """algodesk.__version__ equals 0.0.0."""
    import algodesk

    assert algodesk.__version__ == "0.0.0"


_PACKAGES = [
    "algodesk.api",
    "algodesk.config",
    "algodesk.params",
    "algodesk.registry",
    "algodesk.journal",
    "algodesk.logs",
    "algodesk.ops",
]


def test_all_packages_import() -> None:
    """Every sub-package from the design §2.2 is importable."""
    for pkg in _PACKAGES:
        mod = importlib.import_module(pkg)
        assert mod is not None, f"{pkg} failed to import"
