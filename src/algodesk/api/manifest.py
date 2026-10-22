"""Validated plugin manifest model (pydantic v2, no file I/O)."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from algodesk.api.errors import ManifestError, NotSupported
from algodesk.api.kinds import API_VERSION, Mode, PluginKind

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
NAME_PATTERN = _NAME_RE
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?"
    r"(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$"
)
_ENTRY_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*:[a-zA-Z_][a-zA-Z0-9_]*$")


class PluginManifest(BaseModel):
    """Immutable, strictly-validated plugin manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: PluginKind
    version: str
    api_version: int
    entry: str
    capabilities: dict[str, bool]
    modes_allowed: list[Mode] = [Mode.RESEARCH, Mode.BACKTEST, Mode.PAPER]
    order_capable: bool = False
    limits: dict[str, Any] = {}
    requires: dict[str, Any] = {}
    config_schema: str | None = None
    verified_on: date | None = None
    sources: list[str] = []

    # ------------------------------------------------------------------
    # Field validators
    # ------------------------------------------------------------------

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            msg = (
                f"Name '{v}' must match ^[a-z][a-z0-9_]{{1,40}}$ "
                "(lowercase, 2-41 chars, starts with letter)"
            )
            raise ValueError(msg)
        return v

    @field_validator("version")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        if not _SEMVER_RE.match(v):
            msg = f"Version '{v}' is not valid semver (e.g. 1.0.0)"
            raise ValueError(msg)
        return v

    @field_validator("api_version")
    @classmethod
    def _validate_api_version(cls, v: int) -> int:
        if v != API_VERSION:
            msg = f"api_version must be {API_VERSION}, got {v}"
            raise ValueError(msg)
        return v

    @field_validator("entry")
    @classmethod
    def _validate_entry(cls, v: str) -> str:
        if not _ENTRY_RE.match(v):
            msg = f"Entry '{v}' must be 'module.path:ClassName'"
            raise ValueError(msg)
        return v

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, v: dict[str, bool]) -> dict[str, bool]:
        if not v:
            msg = "capabilities must not be empty"
            raise ValueError(msg)
        return v

    # ------------------------------------------------------------------
    # Model validators (cross-field)
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def _check_order_capable_modes(self) -> PluginManifest:
        if self.order_capable and self.modes_allowed != [Mode.LIVE]:
            msg = (
                "order_capable=True requires modes_allowed=[live]; "
                f"got {[str(m) for m in self.modes_allowed]}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_sources_need_verified_on(self) -> PluginManifest:
        if self.sources and self.verified_on is None:
            msg = "sources is non-empty but verified_on is not set; add a verification date"
            raise ValueError(msg)
        return self

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def supports(self, capability: str) -> bool:
        """Return True if *capability* is declared and enabled."""
        return self.capabilities.get(capability, False)

    def require(self, capability: str) -> None:
        """Raise ``NotSupported`` if *capability* is not declared or disabled."""
        if not self.supports(capability):
            raise NotSupported(
                what=f"Capability '{capability}' is not supported",
                where=f"plugin '{self.name}' (kind={self.kind})",
                fix=f"Set {capability} = true under [capabilities] in plugin.toml",
            )


def parse_manifest(data: dict[str, Any], source: str) -> PluginManifest:
    """Parse *data* into a ``PluginManifest``, wrapping errors in ``ManifestError``.

    Parameters
    ----------
    data : dict
        Raw manifest fields (e.g. from TOML).
    source : str
        Human-readable origin for error messages (e.g. file path).
    """
    try:
        return PluginManifest.model_validate(data)
    except Exception as exc:
        raise ManifestError(
            what=f"Invalid plugin manifest: {exc}",
            where=source,
            fix="Fix the manifest fields listed above and re-validate",
        ) from exc
