"""Tests for algodesk.params public exports and API integrity."""

from __future__ import annotations


def test_params_all_exports_present() -> None:
    """Every expected public name is in __all__ and importable."""
    import algodesk.params

    expected = {
        "ParamRegistry",
        "ParamSpec",
        "ParamType",
        "Tighten",
        "ChangeResult",
        "load_defaults",
        "validate_value",
        "parse_cli_value",
    }
    exported = set(algodesk.params.__all__)
    missing = expected - exported
    assert not missing, f"Missing public exports in algodesk.params.__all__: {missing}"


def test_params_all_names_resolvable() -> None:
    """Every name declared in __all__ is an actual attribute of the module."""
    import algodesk.params

    for name in algodesk.params.__all__:
        attr = getattr(algodesk.params, name, None)
        assert attr is not None, f"Name '{name}' in __all__ but not found in algodesk.params"
