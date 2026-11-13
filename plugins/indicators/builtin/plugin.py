"""Builtin indicators plugin implementation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from algodesk.api import PluginBase
from algodesk.api.manifest import PluginManifest
from algodesk.api.protocols import Indicator, IndicatorSpec

from .indicators import (
    ATR,
    EMA,
    MACD,
    ROC,
    RSI,
    SMA,
    VWAP,
    Bollinger,
    RollingHL,
    VolumeAvg,
)


class BuiltinIndicatorsPlugin(PluginBase):
    """Indicators plugin exposing standard builtin indicators."""

    def __init__(
        self,
        manifest: PluginManifest,
        config: Mapping[str, object] | None = None,
        ctx: Any = None,
    ) -> None:
        super().__init__(manifest, config or {}, ctx)
        ind_name = str(self.config.get("indicator", "sma")).lower()
        cfg_params = {k: v for k, v in self.config.items() if k != "indicator"}
        self._active: Indicator = self.get_indicator(ind_name, **cfg_params)

    @classmethod
    def get_indicator(cls, name: str, **params: Any) -> Indicator:
        """Create an indicator instance by name with given parameters."""
        low = name.lower()
        if low == "sma":
            n = int(params.get("n", params.get("period", 14)))
            return SMA(period=n)
        if low == "ema":
            n = int(params.get("n", params.get("period", 14)))
            return EMA(period=n)
        if low == "rsi":
            n = int(params.get("n", params.get("period", 14)))
            return RSI(period=n)
        if low == "atr":
            n = int(params.get("n", params.get("period", 14)))
            return ATR(n=n)
        if low == "vwap":
            return VWAP()
        if low == "bollinger":
            n = int(params.get("n", params.get("period", 20)))
            k = float(params.get("k", params.get("std_dev", 2.0)))
            return Bollinger(n=n, k=k)
        if low == "macd":
            fast = int(params.get("fast", 12))
            slow = int(params.get("slow", 26))
            signal = int(params.get("signal", 9))
            return MACD(fast=fast, slow=slow, signal=signal)
        if low == "rolling_hl":
            n = int(params.get("n", params.get("period", 20)))
            return RollingHL(n=n)
        if low == "volume_avg":
            n = int(params.get("n", params.get("period", 20)))
            return VolumeAvg(n=n)
        if low == "roc":
            n = int(params.get("n", params.get("period", 12)))
            return ROC(n=n)
        raise ValueError(
            f"Unknown builtin indicator '{name}'. Supported: 'sma', 'ema', 'rsi', "
            f"'atr', 'vwap', 'bollinger', 'macd', 'rolling_hl', 'volume_avg', 'roc'"
        )

    @property
    def spec(self) -> IndicatorSpec:
        """Return the specification of the active indicator."""
        return self._active.spec

    def compute(
        self, values: Sequence[Any] | Mapping[str, Sequence[Any]]
    ) -> Sequence[float | None] | Mapping[str, Sequence[float | None]]:
        """Compute indicator values over a sequence."""
        return self._active.compute(values)

    def new_state(self) -> object:
        """Create fresh state for incremental updates."""
        return self._active.new_state()

    def update(self, state: object, value: Any) -> float | Mapping[str, float | None] | None:
        """Incrementally update indicator calculation."""
        return self._active.update(state, value)


__all__ = [
    "ATR",
    "EMA",
    "MACD",
    "ROC",
    "RSI",
    "SMA",
    "VWAP",
    "Bollinger",
    "BuiltinIndicatorsPlugin",
    "RollingHL",
    "VolumeAvg",
]
