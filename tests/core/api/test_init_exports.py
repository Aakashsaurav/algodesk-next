"""Tests for algodesk.api.__init__ — exports and __all__ completeness."""

from __future__ import annotations

import algodesk.api


def test_api_all_exported_symbols_exist() -> None:
    """Every symbol in __all__ must be resolvable from algodesk.api."""
    for symbol in algodesk.api.__all__:
        assert hasattr(algodesk.api, symbol), f"Symbol '{symbol}' in __all__ not found in api"


def test_api_new_public_contracts_exported() -> None:
    """New T0-2b public types and interfaces must be exported in __all__."""
    required = {
        "Severity",
        "CheckResult",
        "DoctorCheck",
        "ParamValue",
        "ParamReader",
        "JournalWriter",
        "SecretReader",
        "PluginContext",
        "Plugin",
        "PluginBase",
        "Scrubber",
    }
    missing = required - set(algodesk.api.__all__)
    assert not missing, f"Missing exports in algodesk.api.__all__: {missing}"
