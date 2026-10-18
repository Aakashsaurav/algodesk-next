"""Plugin discovery, validation, conformance and safe loading."""

from __future__ import annotations

from algodesk.registry.base_checks import (
    base_contract_problems,
    registry_doctor_checks,
)
from algodesk.registry.conformance import (
    ConformanceStore,
    code_hash,
)
from algodesk.registry.discovery import (
    Issue,
    PluginInfo,
    discover,
)
from algodesk.registry.registry import PluginRegistry
from algodesk.registry.scaffold import scaffold_plugin

__all__ = [
    "ConformanceStore",
    "Issue",
    "PluginInfo",
    "PluginRegistry",
    "base_contract_problems",
    "code_hash",
    "discover",
    "registry_doctor_checks",
    "scaffold_plugin",
]
