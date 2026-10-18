"""Plugin contracts, manifest model and error types.

Public API surface — re-exports everything downstream code needs.
"""

from algodesk.api.checks import CheckResult, DoctorCheck, Severity
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
from algodesk.api.plugin import Plugin, PluginBase
from algodesk.api.scrub import Scrubber
from algodesk.api.services import (
    JournalWriter,
    ParamReader,
    ParamValue,
    PluginContext,
    SecretReader,
)

__all__ = [
    # scrub
    "Scrubber",
    # checks
    "CheckResult",
    "DoctorCheck",
    "Severity",
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
    # plugin
    "Plugin",
    "PluginBase",
    # services
    "JournalWriter",
    "ParamReader",
    "ParamValue",
    "PluginContext",
    "SecretReader",
]
