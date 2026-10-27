"""Tests for PyArrow Parquet schema and market event serialization."""

from __future__ import annotations

from datetime import UTC, datetime

from algodesk.api.market import InstrumentId, Level, Quote, Tick
from algodesk.data.schema import (
    MARKET_EVENT_SCHEMA,
    events_to_table,
    market_event_to_dict,
    row_to_market_event,
)


def test_schema_field_names_and_types() -> None:
    names = MARKET_EVENT_SCHEMA.names
    assert "exch_ts" in names
    assert "recv_ts" in names
    assert "ltp" in names
    assert "ltq" in names
    assert "volume" in names
    assert "oi" in names
    for i in range(1, 6):
        assert f"bid_price_{i}" in names
        assert f"bid_qty_{i}" in names
        assert f"bid_orders_{i}" in names
        assert f"ask_price_{i}" in names
        assert f"ask_qty_{i}" in names
        assert f"ask_orders_{i}" in names


def test_tick_serialization_round_trip() -> None:
    now = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    inst = InstrumentId("NSE:EQ:INFY")
    tick = Tick(
        instrument=inst,
        exch_ts=now,
        recv_ts=now,
        ltp=1500.5,
        ltq=25,
        volume=1000,
        oi=5000,
    )

    d = market_event_to_dict(tick)
    assert d["ltp"] == 1500.5
    assert d["ltq"] == 25
    assert d["volume"] == 1000
    assert d["oi"] == 5000
    assert d["bid_price_1"] is None

    recovered = row_to_market_event(inst, d)
    assert isinstance(recovered, Tick)
    assert recovered.instrument == inst
    assert recovered.ltp == 1500.5
    assert recovered.ltq == 25
    assert recovered.volume == 1000
    assert recovered.oi == 5000


def test_quote_serialization_round_trip() -> None:
    now = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    inst = InstrumentId("NSE:EQ:INFY")
    bids = (
        Level(price=1500.0, qty=100, orders=2),
        Level(price=1499.5, qty=200, orders=5),
    )
    asks = (
        Level(price=1500.5, qty=150, orders=3),
        Level(price=1501.0, qty=300, orders=4),
    )
    quote = Quote(
        instrument=inst,
        exch_ts=now,
        recv_ts=now,
        bids=bids,
        asks=asks,
        ltp=1500.25,
        volume=5000,
    )

    d = market_event_to_dict(quote)
    assert d["bid_price_1"] == 1500.0
    assert d["bid_qty_1"] == 100
    assert d["bid_orders_1"] == 2
    assert d["bid_price_2"] == 1499.5
    assert d["bid_price_3"] is None

    recovered = row_to_market_event(inst, d)
    assert isinstance(recovered, Quote)
    assert len(recovered.bids) == 2
    assert recovered.bids[0].price == 1500.0
    assert recovered.bids[0].qty == 100
    assert recovered.bids[0].orders == 2
    assert len(recovered.asks) == 2
    assert recovered.asks[0].price == 1500.5


def test_events_to_table() -> None:
    now = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    inst = InstrumentId("NSE:EQ:INFY")
    tick1 = Tick(instrument=inst, exch_ts=now, recv_ts=now, ltp=100.0, ltq=10, volume=50)
    tick2 = Tick(instrument=inst, exch_ts=now, recv_ts=now, ltp=101.0, ltq=20, volume=70)

    table = events_to_table([tick1, tick2])
    assert len(table) == 2
    assert table.column("ltp").to_pylist() == [100.0, 101.0]
    assert table.column("volume").to_pylist() == [50, 70]
