"""Tests for algodesk.logs public exports and API integrity."""

from __future__ import annotations

import algodesk.logs


def test_logs_all_exported_symbols_exist() -> None:
    """Every symbol declared in __all__ must exist on algodesk.logs."""
    for symbol in algodesk.logs.__all__:
        assert hasattr(algodesk.logs, symbol), (
            f"Symbol '{symbol}' in __all__ not found in algodesk.logs"
        )


def test_logs_expected_public_names_present() -> None:
    """Expected public names must be exported in algodesk.logs.__all__."""
    expected = {
        "LoggingHandle",
        "get_logger",
        "setup_logging",
    }
    exported = set(algodesk.logs.__all__)
    missing = expected - exported
    assert not missing, f"Missing public exports in algodesk.logs.__all__: {missing}"
