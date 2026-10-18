"""Tests for independence contracts and architectural boundaries."""

from __future__ import annotations

import configparser
from pathlib import Path


def test_importlinter_independence_contract_present() -> None:
    root = Path(__file__).resolve().parents[3]
    linter_cfg = root / ".importlinter"
    assert linter_cfg.is_file()

    parser = configparser.ConfigParser()
    parser.read(linter_cfg)

    # Find independence contract
    independence_sections = [
        sec for sec in parser.sections() if parser.get(sec, "type", fallback="") == "independence"
    ]
    assert len(independence_sections) >= 1, "No independence contract found in .importlinter"

    sec = independence_sections[0]
    modules_text = parser.get(sec, "modules", fallback="")
    modules = [m.strip() for m in modules_text.strip().splitlines() if m.strip()]

    expected = {
        "algodesk.config",
        "algodesk.params",
        "algodesk.registry",
        "algodesk.journal",
        "algodesk.logs",
    }
    for exp in expected:
        assert exp in modules, f"Expected {exp} in independence contract modules"


def test_registry_imports_only_api_and_stdlib() -> None:
    """Ensure algodesk.registry does not import any other core packages or plugins."""
    import importlib
    import sys

    # Unload registry modules if already present
    for k in list(sys.modules.keys()):
        if k.startswith("algodesk.registry"):
            del sys.modules[k]

    importlib.import_module("algodesk.registry")

    forbidden_prefixes = (
        "algodesk.config",
        "algodesk.params",
        "algodesk.journal",
        "algodesk.logs",
        "algodesk.ops",
        "plugins",
    )
    import algodesk.registry.base_checks
    import algodesk.registry.conformance
    import algodesk.registry.discovery
    import algodesk.registry.registry
    import algodesk.registry.scaffold

    for mod in [
        algodesk.registry,
        algodesk.registry.registry,
        algodesk.registry.discovery,
        algodesk.registry.conformance,
        algodesk.registry.scaffold,
        algodesk.registry.base_checks,
    ]:
        for attr_name in dir(mod):
            val = getattr(mod, attr_name)
            if hasattr(val, "__name__") and any(
                str(val.__name__).startswith(p) for p in forbidden_prefixes
            ):
                raise AssertionError(f"Module {mod.__name__} imported forbidden {val.__name__}")
