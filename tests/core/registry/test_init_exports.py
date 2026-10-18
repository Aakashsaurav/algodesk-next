"""Tests for algodesk.registry public exports and namespace completeness."""

from __future__ import annotations


def test_registry_all_exports_present() -> None:
    """Every expected public name is in __all__."""
    import algodesk.registry

    expected = {
        "PluginRegistry",
        "PluginInfo",
        "Issue",
        "discover",
        "code_hash",
        "ConformanceStore",
        "scaffold_plugin",
        "base_contract_problems",
        "registry_doctor_checks",
    }
    exported = set(algodesk.registry.__all__)
    missing = expected - exported
    assert not missing, f"Missing public exports in algodesk.registry.__all__: {missing}"


def test_registry_all_names_resolvable() -> None:
    """Every name in __all__ is an accessible attribute of algodesk.registry."""
    import algodesk.registry

    for name in algodesk.registry.__all__:
        attr = getattr(algodesk.registry, name, None)
        assert attr is not None, f"Name '{name}' in __all__ but not found in algodesk.registry"
