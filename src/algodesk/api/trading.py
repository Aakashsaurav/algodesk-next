"""Trading entities, order state machine, portfolio, and execution events.

All dataclasses are frozen and slotted. Timestamps must be timezone-aware UTC.
Money values use Decimal. Quantities are exact integers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from algodesk.api.errors import SafetyViolation
from algodesk.api.market import InstrumentId, _validate_utc_ts


class Side(StrEnum):
    """Order transaction side."""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    """Order type."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"


class Product(StrEnum):
    """Market product type."""

    CNC = "CNC"
    MIS = "MIS"
    NRML = "NRML"


class Validity(StrEnum):
    """Order time-in-force validity."""

    DAY = "DAY"
    IOC = "IOC"


class OrderState(StrEnum):
    """Order lifecycle state."""

    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"


ORDER_TRANSITIONS: dict[OrderState, set[OrderState]] = {
    OrderState.CREATED: {
        OrderState.SUBMITTED,
        OrderState.REJECTED,
    },
    OrderState.SUBMITTED: {
        OrderState.ACKNOWLEDGED,
        OrderState.REJECTED,
        OrderState.UNKNOWN,
    },
    OrderState.ACKNOWLEDGED: {
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCEL_REQUESTED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
    },
    OrderState.PARTIALLY_FILLED: {
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCEL_REQUESTED,
        OrderState.CANCELLED,
    },
    OrderState.CANCEL_REQUESTED: {
        OrderState.CANCELLED,
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
    },
    OrderState.UNKNOWN: {
        OrderState.RECONCILING,
    },
    OrderState.RECONCILING: {
        OrderState.SUBMITTED,
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
    },
    OrderState.FILLED: set(),
    OrderState.CANCELLED: set(),
    OrderState.REJECTED: set(),
}


def check_transition(from_state: OrderState, to_state: OrderState) -> bool:
    """Validate order state transition; raise SafetyViolation on illegal transition."""
    allowed = ORDER_TRANSITIONS.get(from_state, set())
    if to_state not in allowed:
        raise SafetyViolation(
            what=f"Illegal order transition from {from_state.value} to {to_state.value}",
            where="algodesk.api.trading:check_transition",
            fix="Check order manager transition logic against FR-LIV-1 state machine",
        )
    return True


@dataclass(frozen=True, slots=True)
class OrderSpec:
    """Normalized order specification."""

    instrument: InstrumentId
    side: Side
    qty: int
    type: OrderType
    price: Decimal | None = None
    product: Product = Product.CNC
    validity: Validity = Validity.DAY
    tag: str = ""
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.instrument or str(self.instrument) != str(self.instrument).upper():
            raise ValueError(f"Instrument id must be uppercase: {self.instrument!r}")
        if self.qty <= 0:
            raise ValueError(f"Order quantity must be positive, got {self.qty}")
        if self.type == OrderType.LIMIT:
            if self.price is None or self.price <= Decimal(0):
                raise ValueError(f"Limit order requires positive price, got {self.price!r}")
        if len(self.extensions) > 0:
            raise ValueError("extensions mapping must be empty in M1")


@dataclass(frozen=True, slots=True)
class PlaceOrder:
    """Strategy order placement intent."""

    spec: OrderSpec


@dataclass(frozen=True, slots=True)
class CancelOrder:
    """Strategy order cancellation intent."""

    client_ref: str

    def __post_init__(self) -> None:
        if not self.client_ref:
            raise ValueError("client_ref cannot be empty")


Intent = PlaceOrder | CancelOrder


@dataclass(frozen=True, slots=True)
class Charges:
    """Itemised charges and total for a trade or order."""

    items: Mapping[str, Decimal] = field(default_factory=dict)
    total: Decimal = Decimal("0.00")

    def __post_init__(self) -> None:
        # If total is 0 and items are present, compute sum
        if self.items and self.total == Decimal("0.00"):
            calc_total = sum(self.items.values(), Decimal("0.00"))
            object.__setattr__(self, "total", calc_total)

    @property
    def brokerage(self) -> Decimal:
        return self.items.get("brokerage", Decimal("0.00"))

    @property
    def stt(self) -> Decimal:
        return self.items.get("stt", Decimal("0.00"))

    @property
    def exchange_txn(self) -> Decimal:
        return self.items.get("exchange_txn", Decimal("0.00"))

    @property
    def sebi(self) -> Decimal:
        return self.items.get("sebi", Decimal("0.00"))

    @property
    def stamp(self) -> Decimal:
        return self.items.get("stamp", Decimal("0.00"))

    @property
    def dp(self) -> Decimal:
        return self.items.get("dp", Decimal("0.00"))

    @property
    def gst(self) -> Decimal:
        return self.items.get("gst", Decimal("0.00"))


@dataclass(frozen=True, slots=True)
class Order:
    """Order record tracking state, fills and venue reference."""

    client_ref: str
    spec: OrderSpec
    state: OrderState
    created_ts: datetime
    filled_qty: int = 0
    avg_price: Decimal = Decimal("0.00")
    venue_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.client_ref:
            raise ValueError("client_ref cannot be empty")
        _validate_utc_ts(self.created_ts, "created_ts")
        if self.filled_qty < 0:
            raise ValueError(f"filled_qty cannot be negative: {self.filled_qty}")
        if self.filled_qty > self.spec.qty:
            raise ValueError(
                f"filled_qty ({self.filled_qty}) cannot exceed spec qty ({self.spec.qty})"
            )
        if self.avg_price < Decimal(0):
            raise ValueError(f"avg_price cannot be negative: {self.avg_price}")


@dataclass(frozen=True, slots=True)
class OrderEvent:
    """Order state transition event for journaling and auditing."""

    client_ref: str
    from_state: OrderState
    to_state: OrderState
    ts: datetime
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.client_ref:
            raise ValueError("client_ref cannot be empty")
        _validate_utc_ts(self.ts, "ts")


@dataclass(frozen=True, slots=True)
class Fill:
    """Execution fill details."""

    fill_id: str
    client_ref: str
    qty: int
    price: Decimal
    ts: datetime
    charges: Charges = field(default_factory=Charges)
    instrument: InstrumentId | None = None
    side: Side | None = None
    product: Product | None = None

    def __post_init__(self) -> None:
        if not self.fill_id:
            raise ValueError("fill_id cannot be empty")
        if not self.client_ref:
            raise ValueError("client_ref cannot be empty")
        if self.qty <= 0:
            raise ValueError(f"Fill qty must be positive, got {self.qty}")
        if self.price <= Decimal(0):
            raise ValueError(f"Fill price must be positive, got {self.price}")
        _validate_utc_ts(self.ts, "ts")


@dataclass(frozen=True, slots=True)
class Position:
    """Portfolio position for an instrument."""

    instrument: InstrumentId
    product: Product
    qty: int
    avg_price: Decimal
    realized: Decimal = Decimal("0.00")
    unrealized: Decimal = Decimal("0.00")

    def __post_init__(self) -> None:
        if not self.instrument or str(self.instrument) != str(self.instrument).upper():
            raise ValueError(f"Instrument id must be uppercase: {self.instrument!r}")
        if self.avg_price < Decimal(0):
            raise ValueError(f"avg_price cannot be negative: {self.avg_price}")


@dataclass(frozen=True, slots=True)
class Portfolio:
    """Portfolio snapshot covering cash, positions, equity, and margin."""

    cash: Decimal
    equity: Decimal
    positions: Mapping[str, Position] = field(default_factory=dict)
    margin_used: Decimal = Decimal("0.00")

    def __post_init__(self) -> None:
        if self.margin_used < Decimal(0):
            raise ValueError(f"margin_used cannot be negative: {self.margin_used}")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """Evaluation result from a risk rule or risk engine."""

    allow: bool
    rule: str
    reason: str = ""
    values: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VenueAck:
    """Acknowledgement from execution venue upon place or cancel."""

    client_ref: str
    venue_ref: str
    ts: datetime
    accepted: bool = True
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.client_ref:
            raise ValueError("client_ref cannot be empty")
        _validate_utc_ts(self.ts, "ts")


@dataclass(frozen=True, slots=True)
class VenueEvent:
    """Execution venue event (ack, fill, reject, cancel)."""

    kind: str
    client_ref: str
    ts: datetime
    venue_ref: str | None = None
    fill: Fill | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.client_ref:
            raise ValueError("client_ref cannot be empty")
        _validate_utc_ts(self.ts, "ts")


@dataclass(frozen=True, slots=True)
class Funds:
    """Account funds balance."""

    available: Decimal
    used: Decimal = Decimal("0.00")
    total: Decimal = Decimal("0.00")

    def __post_init__(self) -> None:
        if self.total == Decimal("0.00") and (
            self.available != Decimal("0.00") or self.used != Decimal("0.00")
        ):
            object.__setattr__(self, "total", self.available + self.used)
