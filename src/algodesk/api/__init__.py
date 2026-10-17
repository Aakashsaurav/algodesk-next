"""Plugin contracts, manifest model and error types.

Public API surface — re-exports everything downstream code needs.
"""

from algodesk.api.errors import (
    AlgoDeskError,
    ConfigError,
    JournalError,
    ManifestError,
    NotSupported,
    ParamError,
    PluginError,
    SafetyViolation,
)
from algodesk.api.kinds import API_VERSION, Mode, PluginKind
from algodesk.api.manifest import PluginManifest, parse_manifest

__all__ = [
    # errors
    "AlgoDeskError",
    "ConfigError",
    "JournalError",
    "ManifestError",
    "NotSupported",
    "ParamError",
    "PluginError",
    "SafetyViolation",
    # kinds
    "API_VERSION",
    "Mode",
    "PluginKind",
    # manifest
    "PluginManifest",
    "parse_manifest",
]
