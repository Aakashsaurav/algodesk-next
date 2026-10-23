"""Zerodha NSE fee schedule plugin."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from algodesk.api import PluginBase
from algodesk.api.manifest import PluginManifest


class ScheduleData:
    """Wrapper exposing schedule data attributes and mapping access."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        self.meta = raw.get("meta", {})
        self.components = raw.get("component", [])
        self.taxes = raw.get("tax", [])

    def __getitem__(self, item: str) -> Any:
        return self.raw[item]

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)


class ZerodhaNseFeesPlugin(PluginBase):
    """Fees plugin exposing Zerodha NSE fee and tax schedule."""

    def __init__(
        self,
        manifest: PluginManifest,
        config: Mapping[str, object] | None = None,
        ctx: Any = None,
    ) -> None:
        super().__init__(manifest, config or {}, ctx)
        self._path = Path(__file__).parent / "schedule.toml"

    def schedule(self) -> ScheduleData:
        """Load and return the parsed fee schedule data."""
        with open(self._path, "rb") as f:
            data = tomllib.load(f)
        return ScheduleData(data)
