"""Feature view and batch indicator feature builder for screen engine."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from algodesk.api.market import Bar
from algodesk.api.screens import (
    FeatureView,
    IndicatorResolver,
    SymbolFeatureView,
)


class InMemorySymbolFeatureView(SymbolFeatureView):
    """Read-only view of bars and indicator series for a single symbol."""

    __slots__ = ("_bars", "_indicator_resolver", "_indicators", "_symbol")

    def __init__(
        self,
        symbol: str,
        bars: Sequence[Bar],
        indicators: dict[
            tuple[str, tuple[tuple[str, Any], ...]], dict[str, Sequence[float | None]]
        ],
        indicator_resolver: IndicatorResolver | None = None,
    ) -> None:
        self._symbol = symbol
        self._bars = list(bars)
        self._indicators = indicators
        self._indicator_resolver = indicator_resolver

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def close(self) -> float | None:
        return self._bars[-1].close if self._bars else None

    @property
    def open(self) -> float | None:
        return self._bars[-1].open if self._bars else None

    @property
    def high(self) -> float | None:
        return self._bars[-1].high if self._bars else None

    @property
    def low(self) -> float | None:
        return self._bars[-1].low if self._bars else None

    @property
    def volume(self) -> float | None:
        return self._bars[-1].volume if self._bars else None

    def get_bar(self, field: str, bars_ago: int = 0) -> float | None:
        if not self._bars or bars_ago < 0:
            return None
        idx = len(self._bars) - 1 - bars_ago
        if idx < 0 or idx >= len(self._bars):
            return None
        val = getattr(self._bars[idx], field.lower(), None)
        return float(val) if isinstance(val, (int, float)) else None

    def get_indicator(
        self,
        name: str,
        output: str | None = None,
        bars_ago: int = 0,
        **params: Any,
    ) -> float | None:
        if not self._bars or bars_ago < 0:
            return None
        idx = len(self._bars) - 1 - bars_ago
        if idx < 0 or idx >= len(self._bars):
            return None

        clean_params = {k: v for k, v in params.items() if k != "output"}
        param_key = (name.lower(), tuple(sorted(clean_params.items())))

        # If not already computed and resolver is present, compute on the fly
        if param_key not in self._indicators and self._indicator_resolver is not None:
            self._compute_indicator_on_the_fly(name, clean_params)

        out_map = self._indicators.get(param_key)
        if not out_map:
            return None

        series: Sequence[float | None] | None = None
        if output and output in out_map:
            series = out_map[output]
        elif output:
            # Check lowercase output name
            for k, s in out_map.items():
                if k.lower() == output.lower():
                    series = s
                    break
        else:
            if "value" in out_map:
                series = out_map["value"]
            elif name.lower() in out_map:
                series = out_map[name.lower()]
            elif len(out_map) == 1:
                series = next(iter(out_map.values()))
            else:
                series = next(iter(out_map.values()))

        if series is None or idx >= len(series):
            return None
        val = series[idx]
        if val is None or (isinstance(val, float) and math.isnan(val)):
            return None
        return float(val)

    def _compute_indicator_on_the_fly(self, name: str, params: dict[str, Any]) -> None:
        if self._indicator_resolver is None or not self._bars:
            return
        ind = self._indicator_resolver.get_indicator(name, **params)
        inputs_needed = ("close",)
        spec = getattr(ind, "spec", None)
        if spec and getattr(spec, "inputs", None):
            inputs_needed = tuple(spec.inputs)

        # Build numpy inputs
        c = np.array([b.close for b in self._bars], dtype=np.float64)
        h = np.array([b.high for b in self._bars], dtype=np.float64)
        low_arr = np.array([b.low for b in self._bars], dtype=np.float64)
        o = np.array([b.open for b in self._bars], dtype=np.float64)
        v = np.array([b.volume for b in self._bars], dtype=np.float64)

        if len(inputs_needed) == 1 and inputs_needed[0] == "close":
            res = ind.compute(c)
        else:
            in_dict: dict[str, np.ndarray] = {
                "close": c,
                "high": h,
                "low": low_arr,
                "open": o,
                "volume": v,
            }
            res = ind.compute(in_dict)

        out_map: dict[str, Sequence[float | None]] = {}
        if isinstance(res, Mapping):
            out_map = {k: list(v) for k, v in res.items()}
        elif isinstance(res, Sequence):
            primary = "value"
            if spec and getattr(spec, "outputs", None) and spec.outputs:
                primary = spec.outputs[0]
            out_map = {primary: list(res), "value": list(res)}

        param_key = (name.lower(), tuple(sorted(params.items())))
        self._indicators[param_key] = out_map


class InMemoryFeatureView(FeatureView):
    """Complete FeatureView providing per-symbol views and cross-sectional universe table."""

    def __init__(
        self,
        symbols: Sequence[str],
        views: Mapping[str, InMemorySymbolFeatureView],
    ) -> None:
        self._symbols = list(symbols)
        self._views = dict(views)

    @property
    def symbols(self) -> Sequence[str]:
        return list(self._symbols)

    def for_symbol(self, symbol: str) -> InMemorySymbolFeatureView:
        if symbol not in self._views:
            raise KeyError(f"Symbol '{symbol}' not found in FeatureView")
        return self._views[symbol]

    def get_bar(self, symbol: str, field: str, bars_ago: int = 0) -> float | None:
        if symbol not in self._views:
            return None
        return self._views[symbol].get_bar(field, bars_ago)

    def get_indicator(
        self,
        symbol: str,
        name: str,
        output: str | None = None,
        bars_ago: int = 0,
        **params: Any,
    ) -> float | None:
        if symbol not in self._views:
            return None
        return self._views[symbol].get_indicator(name, output, bars_ago, **params)

    def universe_table(self) -> Mapping[str, Mapping[str, float | None]]:
        table: dict[str, dict[str, float | None]] = {}
        for sym, view in self._views.items():
            row: dict[str, float | None] = {
                "close": view.close,
                "open": view.open,
                "high": view.high,
                "low": view.low,
                "volume": view.volume,
            }
            # Add latest indicator values
            for (ind_name, _), out_dict in view._indicators.items():
                for out_name, series in out_dict.items():
                    col = f"{ind_name}_{out_name}" if out_name != "value" else ind_name
                    val = series[-1] if series else None
                    if val is not None and not (isinstance(val, float) and math.isnan(val)):
                        row[col] = float(val)
                    else:
                        row[col] = None
            table[sym] = row
        return table


def build_feature_view(
    symbols: Sequence[str],
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    required_indicators: Sequence[Mapping[str, Any]] = (),
    indicator_resolver: IndicatorResolver | None = None,
) -> InMemoryFeatureView:
    """Batch-computes required indicators over completed bars using numpy."""
    views: dict[str, InMemorySymbolFeatureView] = {}

    for sym in symbols:
        bars = bars_by_symbol.get(sym, ())
        indicators: dict[
            tuple[str, tuple[tuple[str, Any], ...]], dict[str, Sequence[float | None]]
        ] = {}

        if bars and indicator_resolver is not None:
            c = np.array([b.close for b in bars], dtype=np.float64)
            h = np.array([b.high for b in bars], dtype=np.float64)
            low_arr = np.array([b.low for b in bars], dtype=np.float64)
            o = np.array([b.open for b in bars], dtype=np.float64)
            v = np.array([b.volume for b in bars], dtype=np.float64)
            in_dict: dict[str, np.ndarray] = {
                "close": c,
                "high": h,
                "low": low_arr,
                "open": o,
                "volume": v,
            }

            for req in required_indicators:
                name = str(req.get("ind", "")).lower()
                params = dict(req.get("params", {}))
                params.pop("output", None)
                if not name:
                    continue

                try:
                    ind = indicator_resolver.get_indicator(name, **params)
                    spec = getattr(ind, "spec", None)
                    inputs_needed = (
                        tuple(spec.inputs) if spec and hasattr(spec, "inputs") else ("close",)
                    )

                    if len(inputs_needed) == 1 and inputs_needed[0] == "close":
                        res = ind.compute(c)
                    else:
                        res = ind.compute(in_dict)

                    out_map: dict[str, Sequence[float | None]] = {}
                    if isinstance(res, Mapping):
                        out_map = {k: list(val) for k, val in res.items()}
                    elif isinstance(res, Sequence):
                        primary = "value"
                        if spec and getattr(spec, "outputs", None) and spec.outputs:
                            primary = spec.outputs[0]
                        out_map = {primary: list(res), "value": list(res)}

                    indicators[(name, tuple(sorted(params.items())))] = out_map
                except Exception:
                    # Will be caught during evaluation or skipped by runner
                    pass

        views[sym] = InMemorySymbolFeatureView(
            symbol=sym,
            bars=bars,
            indicators=indicators,
            indicator_resolver=indicator_resolver,
        )

    return InMemoryFeatureView(symbols=symbols, views=views)
