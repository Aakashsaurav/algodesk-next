"""Base plugin protocol and entry class contract."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from algodesk.api.checks import DoctorCheck
from algodesk.api.manifest import PluginManifest
from algodesk.api.services import PluginContext


@runtime_checkable
class Plugin(Protocol):
    """Runtime-checkable protocol that every plugin entry instance must satisfy."""

    manifest: PluginManifest

    def supports(self, capability: str) -> bool:
        """Return True if *capability* is declared and enabled."""
        ...

    def doctor_checks(self) -> Sequence[DoctorCheck]:
        """Return diagnostic checks provided by this plugin."""
        ...


class PluginBase:
    """Base class for AlgoDesk plugins.

    The ``(manifest, config, ctx)`` constructor is the contract for manifest
    entry classes.
    """

    def __init__(
        self,
        manifest: PluginManifest,
        config: Mapping[str, object],
        ctx: PluginContext,
    ) -> None:
        self.manifest = manifest
        self.config = config
        self.ctx = ctx

    def supports(self, capability: str) -> bool:
        """Delegate capability check to the manifest."""
        return self.manifest.supports(capability)

    def require(self, capability: str) -> None:
        """Raise ``NotSupported`` if *capability* is not declared or disabled."""
        self.manifest.require(capability)

    def doctor_checks(self) -> Sequence[DoctorCheck]:
        """Return diagnostic checks for this plugin (default: empty tuple)."""
        return ()
