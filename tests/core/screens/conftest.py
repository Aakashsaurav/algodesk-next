"""Test helpers, fixtures, and fake data readers for screen engine tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from plugins.indicators.builtin.plugin import BuiltinIndicatorsPlugin

from algodesk.api.market import Bar, InstrumentId
from algodesk.api.screens import (
    IndicatorResolver,
    ScreenBars,
    ScreenDataReader,
    ScreenRefusal,
    SkipReason,
)

IST = ZoneInfo("Asia/Kolkata")


class FakeIndicatorResolver(IndicatorResolver):
    """Fake resolver using standard builtin indicators."""

    def get_indicator(self, name: str, **params: Any) -> Any:
        return BuiltinIndicatorsPlugin.get_indicator(name, **params)


class FakeDataReader(ScreenDataReader):
    """In-memory data reader for deterministic, hermetic tests."""

    def __init__(
        self,
        bars_by_symbol: Mapping[str, Sequence[Bar] | ScreenRefusal] | None = None,
        checksum_prefix: str = "csum",
    ) -> None:
        self._data: dict[str, Sequence[Bar] | ScreenRefusal] = dict(bars_by_symbol or {})
        self._checksum_prefix = checksum_prefix

    def add_bars(self, symbol: str, bars: Sequence[Bar]) -> None:
        self._data[symbol] = tuple(bars)

    def add_refusal(self, symbol: str, reason: SkipReason, detail: str) -> None:
        self._data[symbol] = ScreenRefusal(reason=reason, detail=detail)

    def get_bars(
        self,
        instrument: InstrumentId | str,
        timeframe: str,
        start: datetime,
        end: datetime,
        adjust: str = "total",
        accept_unverified: bool = False,
    ) -> ScreenBars | ScreenRefusal:
        sym = str(instrument).split(":")[-1]
        raw_key = str(instrument)

        item = self._data.get(raw_key, self._data.get(sym))
        if item is None:
            return ScreenRefusal(
                reason=SkipReason.NO_DATA, detail=f"No data registered for {instrument}"
            )

        if isinstance(item, ScreenRefusal):
            return item

        # Filter strictly within range
        filtered = tuple(b for b in item if b.end_ts <= end)
        return ScreenBars(
            bars=filtered,
            provenance={
                "dataset_ids": [f"ds_{sym}"],
                "checksums": [f"{self._checksum_prefix}_{sym}"],
                "actions_hash": f"act_hash_{sym}",
                "adjust": adjust,
                "accept_unverified": accept_unverified,
            },
        )


def generate_daily_bars(
    symbol: str,
    prices: Sequence[float],
    as_of: date = date(2026, 11, 12),
    volume: float = 1000.0,
) -> list[Bar]:
    """Generate daily bars ending on as_of for given prices."""
    inst_id = InstrumentId(symbol if ":" in symbol else f"NSE:EQ:{symbol}")
    n = len(prices)
    bars: list[Bar] = []

    for i, p in enumerate(prices):
        day_offset = n - 1 - i
        d = as_of - timedelta(days=day_offset)
        # Session 09:15 to 15:30 IST
        s_dt = datetime.combine(d, time(9, 15), tzinfo=IST).astimezone(UTC)
        e_dt = datetime.combine(d, time(15, 30), tzinfo=IST).astimezone(UTC)
        b = Bar(
            instrument=inst_id,
            timeframe="1d",
            start_ts=s_dt,
            end_ts=e_dt,
            open=float(p),
            high=float(p) * 1.02,
            low=float(p) * 0.98,
            close=float(p),
            volume=float(volume),
        )
        bars.append(b)

    return bars


@pytest.fixture
def fake_resolver() -> FakeIndicatorResolver:
    return FakeIndicatorResolver()
