"""PyArrow Parquet schema and serialization for market events.

Supports ticks and 5-level order book quotes with nullable depth fields.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pyarrow as pa

from algodesk.api.market import FeedStatus, InstrumentId, Level, MarketEvent, Quote, Tick

# Define PyArrow schema for ticks and 5-level quotes
_FIELDS: list[pa.Field] = [
    pa.field("exch_ts", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("recv_ts", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("ltp", pa.float64(), nullable=False),
    pa.field("ltq", pa.int64(), nullable=False),
    pa.field("volume", pa.int64(), nullable=False),
    pa.field("oi", pa.int64(), nullable=True),
]

for i in range(1, 6):
    _FIELDS.append(pa.field(f"bid_price_{i}", pa.float64(), nullable=True))
    _FIELDS.append(pa.field(f"bid_qty_{i}", pa.int64(), nullable=True))
    _FIELDS.append(pa.field(f"bid_orders_{i}", pa.int64(), nullable=True))

for i in range(1, 6):
    _FIELDS.append(pa.field(f"ask_price_{i}", pa.float64(), nullable=True))
    _FIELDS.append(pa.field(f"ask_qty_{i}", pa.int64(), nullable=True))
    _FIELDS.append(pa.field(f"ask_orders_{i}", pa.int64(), nullable=True))

MARKET_EVENT_SCHEMA = pa.schema(_FIELDS)


def market_event_to_dict(event: MarketEvent) -> dict[str, Any]:
    """Convert Tick or Quote to dictionary matching MARKET_EVENT_SCHEMA."""
    if isinstance(event, FeedStatus):
        msg = f"Cannot serialize FeedStatus to market event dictionary: {event}"
        raise ValueError(msg)
    row: dict[str, Any] = {
        "exch_ts": event.exch_ts,
        "recv_ts": event.recv_ts,
        "ltp": float(event.ltp),
        "ltq": int(getattr(event, "ltq", 0)),
        "volume": int(event.volume),
        "oi": getattr(event, "oi", None),
    }

    bids = getattr(event, "bids", ())
    for i in range(1, 6):
        if i <= len(bids):
            level = bids[i - 1]
            row[f"bid_price_{i}"] = float(level.price)
            row[f"bid_qty_{i}"] = int(level.qty)
            row[f"bid_orders_{i}"] = int(level.orders)
        else:
            row[f"bid_price_{i}"] = None
            row[f"bid_qty_{i}"] = None
            row[f"bid_orders_{i}"] = None

    asks = getattr(event, "asks", ())
    for i in range(1, 6):
        if i <= len(asks):
            level = asks[i - 1]
            row[f"ask_price_{i}"] = float(level.price)
            row[f"ask_qty_{i}"] = int(level.qty)
            row[f"ask_orders_{i}"] = int(level.orders)
        else:
            row[f"ask_price_{i}"] = None
            row[f"ask_qty_{i}"] = None
            row[f"ask_orders_{i}"] = None

    return row


def row_to_market_event(instrument: InstrumentId, row: dict[str, Any]) -> Tick | Quote:
    """Reconstruct Tick or Quote from a dictionary."""
    exch_ts = row["exch_ts"]
    if isinstance(exch_ts, datetime) and exch_ts.tzinfo is None:
        exch_ts = exch_ts.replace(tzinfo=UTC)
    recv_ts = row["recv_ts"]
    if isinstance(recv_ts, datetime) and recv_ts.tzinfo is None:
        recv_ts = recv_ts.replace(tzinfo=UTC)

    ltp = float(row["ltp"])
    volume = int(row["volume"])
    has_depth = row.get("bid_price_1") is not None or row.get("ask_price_1") is not None

    if has_depth:
        bids_list: list[Level] = []
        for i in range(1, 6):
            p = row.get(f"bid_price_{i}")
            if p is not None:
                q = int(row.get(f"bid_qty_{i}", 0) or 0)
                o = int(row.get(f"bid_orders_{i}", 1) or 1)
                bids_list.append(Level(price=float(p), qty=q, orders=o))

        asks_list: list[Level] = []
        for i in range(1, 6):
            p = row.get(f"ask_price_{i}")
            if p is not None:
                q = int(row.get(f"ask_qty_{i}", 0) or 0)
                o = int(row.get(f"ask_orders_{i}", 1) or 1)
                asks_list.append(Level(price=float(p), qty=q, orders=o))

        return Quote(
            instrument=instrument,
            exch_ts=exch_ts,
            recv_ts=recv_ts,
            bids=tuple(bids_list),
            asks=tuple(asks_list),
            ltp=ltp,
            volume=volume,
        )

    return Tick(
        instrument=instrument,
        exch_ts=exch_ts,
        recv_ts=recv_ts,
        ltp=ltp,
        ltq=int(row.get("ltq", 0) or 0),
        volume=volume,
        oi=row.get("oi"),
    )


def events_to_table(events: Sequence[MarketEvent]) -> pa.Table:
    """Convert sequence of MarketEvents to PyArrow Table."""
    if not events:
        return pa.Table.from_batches([], schema=MARKET_EVENT_SCHEMA)

    columns: dict[str, list[Any]] = {field.name: [] for field in MARKET_EVENT_SCHEMA}
    for ev in events:
        d = market_event_to_dict(ev)
        for k, v in d.items():
            columns[k].append(v)

    return pa.Table.from_pydict(columns, schema=MARKET_EVENT_SCHEMA)
