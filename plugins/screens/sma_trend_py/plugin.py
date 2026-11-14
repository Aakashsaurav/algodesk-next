"""SMA trend test screen plugin in Python."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from algodesk.api.plugin import PluginBase
from algodesk.api.screens import FeatureView, ScreenRow, ScreenSpec


class SmaTrendPyPlugin(PluginBase):
    """Screen plugin implementing SMA(50) > SMA(200) on 1d timeframe."""

    spec = ScreenSpec(
        name="sma_trend_test",
        description="Test screen: SMA(50) > SMA(200) on 1d timeframe",
        timeframe="1d",
        adjust="total",
        output_columns=("sma_50", "sma_200"),
        required_indicators=(
            {"ind": "sma", "params": {"n": 50}},
            {"ind": "sma", "params": {"n": 200}},
        ),
    )
    # Match the TOML test screen file hash so result hashes match identically
    screen_file_hash = "31c99b015be10f63420551958e8a3e55eda0fba117b6b7e70643b00f4f71cbb8"

    def __init__(
        self,
        manifest: Any = None,
        config: Mapping[str, object] | None = None,
        ctx: Any = None,
    ) -> None:
        super().__init__(manifest, config or {}, ctx)

    def evaluate(self, features: FeatureView) -> Sequence[ScreenRow]:
        rows: list[ScreenRow] = []
        for sym in features.symbols:
            sym_view = features.for_symbol(sym)
            sma50 = sym_view.get_indicator("sma", n=50)
            sma200 = sym_view.get_indicator("sma", n=200)
            passed = sma50 is not None and sma200 is not None and sma50 > sma200
            score = 100.0 if passed else 0.0
            rows.append(
                ScreenRow(
                    symbol=sym,
                    passed=passed,
                    score=score,
                    values={"sma_50": sma50, "sma_200": sma200},
                )
            )
        return rows
