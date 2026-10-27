"""Tests for DataService pass-through API."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal

from algodesk.api.market import Bar, Instrument, InstrumentId, SeriesKey
from algodesk.data.service import DataService


class _DummyDataSource:
    def __init__(self, name: str) -> None:
        self.name = name

    def instruments(self) -> Iterable[Instrument]:
        return [
            Instrument(
                id=InstrumentId("NSE:EQ:INFY"),
                exchange="NSE",
                segment="EQ",
                symbol="INFY",
                kind="equity",
                lot_size=1,
                tick_size=Decimal("0.05"),
            )
        ]

    def history(self, key: SeriesKey, start: datetime, end: datetime) -> Iterable[Bar]:
        return [
            Bar(
                instrument=key.instrument,
                timeframe=key.timeframe,
                start_ts=start,
                end_ts=end,
                open=100.0,
                high=105.0,
                low=99.0,
                close=103.0,
                volume=1000,
                complete=True,
            )
        ]


def test_data_service_routing() -> None:
    src_yf = _DummyDataSource("yfinance")
    src_up = _DummyDataSource("upstox")
    service = DataService(sources={"yfinance": src_yf, "upstox": src_up}, default_source="yfinance")

    insts = service.instruments()
    assert len(insts) == 1
    assert insts[0].symbol == "INFY"

    t0 = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    t1 = datetime(2026, 10, 26, 9, 16, 0, tzinfo=UTC)
    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:INFY"),
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="yfinance",
    )

    bars = service.bars(key, t0, t1)
    assert len(bars) == 1
    assert bars[0].close == 103.0
