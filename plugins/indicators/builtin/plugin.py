"""Builtin indicators plugin implementation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from algodesk.api import PluginBase
from algodesk.api.manifest import PluginManifest
from algodesk.api.protocols import Indicator, IndicatorSpec

from .indicators import EMA, RSI, SMA


class BuiltinIndicatorsPlugin(PluginBase):
    """Indicators plugin exposing standard SMA, EMA, and Wilder's RSI."""

    def __init__(
        self,
        manifest: PluginManifest,
        config: Mapping[str, object] | None = None,
        ctx: Any = None,
    ) -> None:
        super().__init__(manifest, config or {}, ctx)
        ind_name = str(self.config.get("indicator", "sma")).lower()
        period = int(self.config.get("period", 14))  # type: ignore[arg-type]
        self._active: Indicator = self.get_indicator(ind_name, period=period)

    @classmethod
    def get_indicator(cls, name: str, **params: Any) -> Indicator:
        """Create an indicator instance by name with given parameters."""
        low = name.lower()
        if low == "sma":
            return SMA(period=int(params.get("period", 14)))
        if low == "ema":
            return EMA(period=int(params.get("period", 14)))
        if low == "rsi":
            return RSI(period=int(params.get("period", 14)))
        raise ValueError(f"Unknown builtin indicator '{name}'. Supported: 'sma', 'ema', 'rsi'")

    @property
    def spec(self) -> IndicatorSpec:
        """Return the specification of the active indicator."""
        return self._active.spec

    def compute(self, values: Sequence[float]) -> Sequence[float | None]:
        """Compute indicator values over a sequence."""
        return self._active.compute(values)

    def new_state(self) -> object:
        """Create fresh state for incremental updates."""
        return self._active.new_state()

    def update(self, state: object, value: float) -> float | None:
        """Incrementally update indicator calculation."""
        return self._active.update(state, value)


__all__ = ["BuiltinIndicatorsPlugin", "EMA", "RSI", "SMA"]
