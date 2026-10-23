"""Market data types, identifiers, and price conversion helpers.

All dataclasses are frozen and slotted. Timestamps must be timezone-aware UTC.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import NewType

InstrumentId = NewType("InstrumentId", str)


def _validate_utc_ts(ts: datetime, field_name: str) -> None:
    if not isinstance(ts, datetime):
        raise TypeError(f"{field_name} must be a datetime, got {type(ts).__name__}")
    if ts.tzinfo is None or ts.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC datetime: {ts!r}")


@dataclass(frozen=True, slots=True)
class IdParts:
    """Parsed components of an InstrumentId."""

    exchange: str
    segment: str
    symbol: str
    expiry: date | None = None
    strike: Decimal | None = None
    option_type: str | None = None

    def __post_init__(self) -> None:
        if not self.exchange or self.exchange != self.exchange.upper():
            raise ValueError(f"Exchange must be non-empty uppercase string: {self.exchange!r}")
        if not self.segment or self.segment != self.segment.upper():
            raise ValueError(f"Segment must be non-empty uppercase string: {self.segment!r}")
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError(f"Symbol must be non-empty uppercase string: {self.symbol!r}")
        if self.option_type is not None:
            if self.option_type not in ("CE", "PE"):
                raise ValueError(f"Option type must be 'CE' or 'PE', got {self.option_type!r}")
            if self.strike is None or self.strike <= Decimal(0):
                raise ValueError("Strike must be a positive Decimal when option_type is specified")
            if self.expiry is None:
                raise ValueError("Expiry must be specified when option_type is present")
        if self.strike is not None and self.option_type is None:
            raise ValueError("Option type must be specified when strike is present")


def parse_id(id_str: str | InstrumentId) -> IdParts:
    """Parse a colon-separated uppercase instrument ID string into IdParts."""
    if not isinstance(id_str, str):
        raise TypeError(f"Instrument id must be a string, got {type(id_str).__name__}")
    raw = id_str.strip()
    if not raw or raw != raw.upper():
        raise ValueError(f"Instrument id must be non-empty uppercase string: {id_str!r}")

    parts = raw.split(":")
    if len(parts) == 3:
        # EXCHANGE:SEGMENT:SYMBOL
        return IdParts(exchange=parts[0], segment=parts[1], symbol=parts[2])
    if len(parts) == 4:
        # EXCHANGE:SEGMENT:SYMBOL:EXPIRY
        try:
            exp = date.fromisoformat(parts[3])
        except ValueError as err:
            raise ValueError(f"Invalid expiry date in instrument id '{id_str}': {err}") from None
        return IdParts(exchange=parts[0], segment=parts[1], symbol=parts[2], expiry=exp)
    if len(parts) == 6:
        # EXCHANGE:SEGMENT:SYMBOL:EXPIRY:STRIKE:TYPE
        try:
            exp = date.fromisoformat(parts[3])
        except ValueError as err:
            raise ValueError(f"Invalid expiry date in instrument id '{id_str}': {err}") from None
        try:
            strike = Decimal(parts[4])
        except Exception as err:
            raise ValueError(f"Invalid strike in instrument id '{id_str}': {err}") from None
        opt_type = parts[5]
        return IdParts(
            exchange=parts[0],
            segment=parts[1],
            symbol=parts[2],
            expiry=exp,
            strike=strike,
            option_type=opt_type,
        )

    raise ValueError(
        f"Invalid instrument id format '{id_str}'. Expected 3, 4, or 6 colon-separated components."
    )


def format_id(parts: IdParts) -> InstrumentId:
    """Format an IdParts object into a canonical InstrumentId."""
    base = f"{parts.exchange}:{parts.segment}:{parts.symbol}"
    if parts.expiry is not None:
        base += f":{parts.expiry.isoformat()}"
        if parts.strike is not None and parts.option_type is not None:
            base += f":{parts.strike}:{parts.option_type}"
    return InstrumentId(base)


@dataclass(frozen=True, slots=True)
class Instrument:
    """Financial instrument definition."""

    id: InstrumentId
    exchange: str
    segment: str
    symbol: str
    kind: str
    lot_size: int
    tick_size: Decimal
    expiry: date | None = None
    underlying: str | None = None
    isin: str | None = None
    broker_keys: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id or str(self.id) != str(self.id).upper():
            raise ValueError(f"Instrument id must be uppercase: {self.id!r}")
        if self.lot_size <= 0:
            raise ValueError(f"lot_size must be positive, got {self.lot_size}")
        if not isinstance(self.tick_size, Decimal) or self.tick_size <= Decimal(0):
            raise ValueError(f"tick_size must be a positive Decimal, got {self.tick_size!r}")


@dataclass(frozen=True, slots=True)
class SeriesKey:
    """Time-series stream key."""

    instrument: InstrumentId
    kind: str
    timeframe: str
    adjustment: str
    source: str

    def __post_init__(self) -> None:
        if not self.instrument or str(self.instrument) != str(self.instrument).upper():
            raise ValueError(f"Instrument id must be uppercase: {self.instrument!r}")
        if self.kind not in ("bars", "ticks", "depth"):
            raise ValueError(f"Invalid series kind '{self.kind}'. Expected bars, ticks, or depth")
        if self.adjustment not in ("raw", "adjusted"):
            raise ValueError(f"Invalid adjustment '{self.adjustment}'. Expected raw or adjusted")


@dataclass(frozen=True, slots=True)
class Level:
    """Order book price level."""

    price: float
    qty: int
    orders: int = 1

    def __post_init__(self) -> None:
        if self.price < 0.0:
            raise ValueError(f"price cannot be negative: {self.price}")
        if self.qty < 0:
            raise ValueError(f"qty cannot be negative: {self.qty}")
        if self.orders < 0:
            raise ValueError(f"orders cannot be negative: {self.orders}")


@dataclass(frozen=True, slots=True)
class Tick:
    """Trade tick event."""

    instrument: InstrumentId
    exch_ts: datetime
    recv_ts: datetime
    ltp: float
    ltq: int
    volume: int
    oi: int | None = None

    def __post_init__(self) -> None:
        if not self.instrument or str(self.instrument) != str(self.instrument).upper():
            raise ValueError(f"Instrument id must be uppercase: {self.instrument!r}")
        _validate_utc_ts(self.exch_ts, "exch_ts")
        _validate_utc_ts(self.recv_ts, "recv_ts")
        if self.ltp < 0.0:
            raise ValueError(f"ltp cannot be negative: {self.ltp}")
        if self.ltq <= 0:
            raise ValueError(f"ltq must be positive: {self.ltq}")
        if self.volume < 0:
            raise ValueError(f"volume cannot be negative: {self.volume}")
        if self.oi is not None and self.oi < 0:
            raise ValueError(f"oi cannot be negative: {self.oi}")


@dataclass(frozen=True, slots=True)
class Quote:
    """Order book quote with up to 5 levels of depth."""

    instrument: InstrumentId
    exch_ts: datetime
    recv_ts: datetime
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    ltp: float
    volume: int

    def __post_init__(self) -> None:
        if not self.instrument or str(self.instrument) != str(self.instrument).upper():
            raise ValueError(f"Instrument id must be uppercase: {self.instrument!r}")
        _validate_utc_ts(self.exch_ts, "exch_ts")
        _validate_utc_ts(self.recv_ts, "recv_ts")
        if len(self.bids) > 5:
            raise ValueError(f"bids depth exceeds maximum 5 levels: {len(self.bids)}")
        if len(self.asks) > 5:
            raise ValueError(f"asks depth exceeds maximum 5 levels: {len(self.asks)}")

        # Validate positive quantities in depth levels
        for b in self.bids:
            if b.qty <= 0:
                raise ValueError(f"Bid level qty must be positive, got {b.qty}")
        for a in self.asks:
            if a.qty <= 0:
                raise ValueError(f"Ask level qty must be positive, got {a.qty}")

        # Validate bid ordering: descending by price
        for i in range(len(self.bids) - 1):
            if self.bids[i].price < self.bids[i + 1].price:
                p0 = self.bids[i].price
                p1 = self.bids[i + 1].price
                raise ValueError(f"Bids must be ordered descending by price: {p0} < {p1}")

        # Validate ask ordering: ascending by price
        for i in range(len(self.asks) - 1):
            if self.asks[i].price > self.asks[i + 1].price:
                p0 = self.asks[i].price
                p1 = self.asks[i + 1].price
                raise ValueError(f"Asks must be ordered ascending by price: {p0} > {p1}")

        # Validate best bid <= best ask if both present
        if self.bids and self.asks and self.bids[0].price > self.asks[0].price:
            raise ValueError(
                f"Crossed market: best bid ({self.bids[0].price}) > best ask ({self.asks[0].price})"
            )

        if self.ltp < 0.0:
            raise ValueError(f"ltp cannot be negative: {self.ltp}")
        if self.volume < 0:
            raise ValueError(f"volume cannot be negative: {self.volume}")


@dataclass(frozen=True, slots=True)
class Bar:
    """OHLCV candlestick bar."""

    instrument: InstrumentId
    timeframe: str
    start_ts: datetime
    end_ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    complete: bool = False

    def __post_init__(self) -> None:
        if not self.instrument or str(self.instrument) != str(self.instrument).upper():
            raise ValueError(f"Instrument id must be uppercase: {self.instrument!r}")
        _validate_utc_ts(self.start_ts, "start_ts")
        _validate_utc_ts(self.end_ts, "end_ts")
        if self.start_ts > self.end_ts:
            raise ValueError(f"start_ts ({self.start_ts}) cannot be after end_ts ({self.end_ts})")
        if self.low < 0.0:
            raise ValueError(f"low price cannot be negative: {self.low}")
        if self.low > self.high:
            raise ValueError(f"low ({self.low}) cannot exceed high ({self.high})")
        if self.open < self.low or self.open > self.high:
            raise ValueError(f"open ({self.open}) outside [low={self.low}, high={self.high}]")
        if self.close < self.low or self.close > self.high:
            raise ValueError(f"close ({self.close}) outside [low={self.low}, high={self.high}]")
        if self.volume < 0:
            raise ValueError(f"volume cannot be negative: {self.volume}")


@dataclass(frozen=True, slots=True)
class FeedStatus:
    """Status update from a market data feed."""

    state: str
    ts: datetime
    details: str = ""
    gap_start: datetime | None = None
    gap_end: datetime | None = None

    def __post_init__(self) -> None:
        if self.state not in ("connected", "disconnected", "gap"):
            raise ValueError(
                f"Invalid feed state '{self.state}'. Expected 'connected', 'disconnected', or 'gap'"
            )
        _validate_utc_ts(self.ts, "ts")
        if self.gap_start is not None:
            _validate_utc_ts(self.gap_start, "gap_start")
        if self.gap_end is not None:
            _validate_utc_ts(self.gap_end, "gap_end")
        if self.gap_start is not None and self.gap_end is not None:
            if self.gap_start > self.gap_end:
                raise ValueError(
                    f"gap_start ({self.gap_start}) cannot be after gap_end ({self.gap_end})"
                )


MarketEvent = Tick | Quote | FeedStatus


def to_price(value: float, tick: Decimal) -> Decimal:
    """Round a float price to the nearest tick grid using Decimal ROUND_HALF_UP."""
    if not isinstance(tick, Decimal) or tick <= Decimal(0):
        raise ValueError(f"tick must be a positive Decimal, got {tick!r}")
    val_dec = Decimal(str(value))
    steps = (val_dec / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    price = steps * tick
    return price.quantize(tick)
