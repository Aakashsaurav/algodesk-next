"""Tests for algodesk.journal public exports and API integrity."""

from __future__ import annotations

import algodesk.journal


def test_journal_all_exported_symbols_exist() -> None:
    """Every symbol declared in __all__ must exist on algodesk.journal."""
    for symbol in algodesk.journal.__all__:
        assert hasattr(algodesk.journal, symbol), (
            f"Symbol '{symbol}' in __all__ not found in algodesk.journal"
        )


def test_journal_expected_public_names_present() -> None:
    """Expected public names must be exported in algodesk.journal.__all__."""
    expected = {
        "GENESIS_HASH",
        "Journal",
        "JournalReader",
        "JournalSettings",
        "VerifyResult",
        "events",
        "stats",
        "tail",
        "unclosed_runs",
        "verify_journal",
    }
    exported = set(algodesk.journal.__all__)
    missing = expected - exported
    assert not missing, f"Missing public exports in algodesk.journal.__all__: {missing}"
