"""PyArrow Parquet schema and serialization for market events.

Supports ticks and 5-level order book quotes with nullable depth fields.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

from algodesk.api.market import (
    Bar,
    FeedStatus,
    InstrumentId,
    Level,
    MarketEvent,
    Quote,
    Tick,
)

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


_DEFAULT_EXCH_TZ = ZoneInfo("Asia/Kolkata")

BAR_FIELDS: list[pa.Field] = [
    pa.field("start_ts", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("end_ts", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("exch_ts", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("exch_time", pa.string(), nullable=False),
    pa.field("open", pa.float64(), nullable=False),
    pa.field("high", pa.float64(), nullable=False),
    pa.field("low", pa.float64(), nullable=False),
    pa.field("close", pa.float64(), nullable=False),
    pa.field("volume", pa.int64(), nullable=False),
    pa.field("complete", pa.bool_(), nullable=False),
    pa.field("synthetic", pa.bool_(), nullable=False),
]

BAR_SCHEMA = pa.schema(BAR_FIELDS)


def bar_to_dict(bar: Bar, exchange_tz: tzinfo | None = None) -> dict[str, Any]:
    """Convert Bar to dictionary matching BAR_SCHEMA with UTC and exchange-time column."""
    tz = exchange_tz or _DEFAULT_EXCH_TZ
    exch_dt = bar.start_ts.astimezone(tz)
    return {
        "start_ts": bar.start_ts,
        "end_ts": bar.end_ts,
        "exch_ts": bar.start_ts,
        "exch_time": exch_dt.isoformat(),
        "open": float(bar.open),
        "high": float(bar.high),
        "low": float(bar.low),
        "close": float(bar.close),
        "volume": int(bar.volume),
        "complete": bool(getattr(bar, "complete", False)),
        "synthetic": bool(getattr(bar, "synthetic", False)),
    }


def row_to_bar(instrument: InstrumentId, timeframe: str, row: dict[str, Any]) -> Bar:
    """Reconstruct Bar from dictionary row."""
    start_ts = row["start_ts"]
    if isinstance(start_ts, datetime) and start_ts.tzinfo is None:
        start_ts = start_ts.replace(tzinfo=UTC)
    end_ts = row["end_ts"]
    if isinstance(end_ts, datetime) and end_ts.tzinfo is None:
        end_ts = end_ts.replace(tzinfo=UTC)

    return Bar(
        instrument=instrument,
        timeframe=timeframe,
        start_ts=start_ts,
        end_ts=end_ts,
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=int(row["volume"]),
        complete=bool(row.get("complete", False)),
        synthetic=bool(row.get("synthetic", False)),
    )


def bars_to_table(
    bars: Sequence[Bar],
    exchange_tz: tzinfo | None = None,
) -> pa.Table:
    """Convert sequence of Bar objects to sorted PyArrow Table."""
    if not bars:
        return pa.Table.from_batches([], schema=BAR_SCHEMA)

    # Sort strictly by start_ts ascending
    sorted_bars = sorted(bars, key=lambda b: b.start_ts)
    cols: dict[str, list[Any]] = {field.name: [] for field in BAR_SCHEMA}
    for b in sorted_bars:
        d = bar_to_dict(b, exchange_tz=exchange_tz)
        for k, v in d.items():
            cols[k].append(v)

    return pa.Table.from_pydict(cols, schema=BAR_SCHEMA)


def table_to_bars(
    table: pa.Table,
    instrument: InstrumentId,
    timeframe: str,
) -> list[Bar]:
    """Convert PyArrow Table to list of Bar objects."""
    pydict = table.to_pydict()
    if not pydict or "start_ts" not in pydict:
        return []
    start_ts_col = pydict["start_ts"]
    n = len(start_ts_col)
    end_ts_col = pydict.get("end_ts", start_ts_col)
    open_col = pydict.get("open")
    high_col = pydict.get("high")
    low_col = pydict.get("low")
    close_col = pydict.get("close")
    vol_col = pydict.get("volume")
    comp_col = pydict.get("complete")
    synth_col = pydict.get("synthetic")

    bars: list[Bar] = []
    for i in range(n):
        s_ts = start_ts_col[i]
        if isinstance(s_ts, datetime) and s_ts.tzinfo is None:
            s_ts = s_ts.replace(tzinfo=UTC)
        e_ts = end_ts_col[i] if end_ts_col is not None else s_ts
        if isinstance(e_ts, datetime) and e_ts.tzinfo is None:
            e_ts = e_ts.replace(tzinfo=UTC)
        bars.append(
            Bar(
                instrument=instrument,
                timeframe=timeframe,
                start_ts=s_ts,
                end_ts=e_ts,
                open=float(open_col[i]) if open_col is not None else 0.0,
                high=float(high_col[i]) if high_col is not None else 0.0,
                low=float(low_col[i]) if low_col is not None else 0.0,
                close=float(close_col[i]) if close_col is not None else 0.0,
                volume=int(vol_col[i]) if vol_col is not None else 0,
                complete=bool(comp_col[i]) if comp_col is not None else True,
                synthetic=bool(synth_col[i]) if synth_col is not None else False,
            )
        )
    return bars


def write_bars_parquet(
    path: Path | str,
    bars: Sequence[Bar],
    exchange_tz: tzinfo | None = None,
) -> None:
    """Write bars to Parquet file compressed with zstd and sorted by timestamp."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    table = bars_to_table(bars, exchange_tz=exchange_tz)
    pq.write_table(table, p, compression="zstd")


def read_bars_parquet(
    path: Path | str,
    instrument: InstrumentId,
    timeframe: str,
    columns: Sequence[str] | None = None,
) -> list[Bar]:
    """Read Parquet file and parse rows into Bar objects."""
    p = Path(path)
    if not p.exists():
        return []
    cols = list(columns) if columns else None
    table = pq.read_table(p, columns=cols)
    return table_to_bars(table, instrument, timeframe)
