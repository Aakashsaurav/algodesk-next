"""Kind interfaces and Protocols for plugins, runtime engines, and adapters.

Pure types and Protocols with no business logic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from algodesk.api.market import (
    ActionRecord,
    Bar,
    Instrument,
    InstrumentId,
    MarketEvent,
    Quote,
    SeriesKey,
    Tick,
)
from algodesk.api.trading import (
    Fill,
    Funds,
    Intent,
    Order,
    OrderSpec,
    OrderStatus,
    Position,
    RiskDecision,
    VenueAck,
    VenueEvent,
)


@dataclass(frozen=True, slots=True)
class LoginRequest:
    """Manual or guided interactive login request."""

    url: str
    state: str = ""
    instructions: str = ""


@dataclass(frozen=True, slots=True)
class AuthSession:
    """Authenticated session credentials and expiry."""

    token: str
    expires_at: datetime | None = None
    user_id: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IndicatorSpec:
    """Metadata specification for a technical indicator."""

    name: str
    inputs: tuple[str, ...] = ("close",)
    params_schema: Mapping[str, Any] = field(default_factory=dict)
    outputs: tuple[str, ...] = ("value",)
    warmup: int = 0
    timeframes: tuple[str, ...] | None = None


@runtime_checkable
class Clock(Protocol):
    """Injected system or simulation clock."""

    def now(self) -> datetime:
        """Current timezone-aware UTC datetime."""
        ...

    def monotonic_ns(self) -> int:
        """Monotonic nanosecond timestamp."""
        ...


class SystemClock:
    """Standard system clock providing timezone-aware UTC datetime and monotonic ns."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        import time

        return time.monotonic_ns()


@dataclass(frozen=True, slots=True)
class TradingSession:
    """Market operating session with timezone-aware start and end times."""

    name: str
    start: datetime
    end: datetime
    tradable: bool = True

    @property
    def open(self) -> datetime:
        return self.start

    @property
    def close(self) -> datetime:
        return self.end


Session = TradingSession


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Read-only context provided to strategies during execution."""

    clock: Any = None
    instruments: Mapping[InstrumentId, Instrument] = field(default_factory=dict)
    bars: Callable[..., Sequence[Bar]] | None = None
    indicator: Callable[..., Any] | None = None
    position: Callable[[InstrumentId | str], Position | None] | None = None
    portfolio: Any = None
    params: Mapping[str, Any] = field(default_factory=dict)
    risk_state: Mapping[str, Any] = field(default_factory=dict)
    log: Any = None


@dataclass(frozen=True, slots=True)
class RiskView:
    """Current risk and exposure view passed to risk rules."""

    portfolio: Any = None
    positions: Mapping[str, Position] = field(default_factory=dict)
    open_orders: Sequence[Order] = field(default_factory=list)
    daily_pnl: Decimal = Decimal("0.00")
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Message:
    """Outbound notification message."""

    text: str
    severity: str = "INFO"
    title: str = ""
    channel: str = ""
    ts: datetime | None = None


@runtime_checkable
class AuthFlow(Protocol):
    """Authentication and session management flow."""

    def begin(self) -> LoginRequest: ...

    def begin_auto(self, *, timeout_s: float) -> AuthSession: ...

    def complete(self, code: str) -> AuthSession: ...

    def use_token(self, token: str, expires: date | None) -> AuthSession: ...

    def validate(self, session: AuthSession) -> bool: ...

    def expires_at(self, session: AuthSession) -> datetime | None: ...


@runtime_checkable
class DataSource(Protocol):
    """Market data provider protocol."""

    def instruments(self) -> Iterable[Instrument]: ...

    def history(self, key: SeriesKey, start: datetime, end: datetime) -> Iterable[Bar]: ...

    def stream(self, instruments: Sequence[InstrumentId]) -> AsyncIterator[MarketEvent]: ...


@runtime_checkable
class ExecutionVenue(Protocol):
    """Execution venue protocol for placing orders and receiving fills."""

    def configure(self, instruments: Sequence[InstrumentId]) -> None: ...

    async def place(self, spec: OrderSpec, client_ref: str) -> VenueAck: ...

    async def cancel(self, client_ref: str) -> VenueAck: ...

    def order_status(self, client_ref: str) -> OrderStatus | None: ...

    def end_of_day(self) -> None: ...

    def on_quote(self, quote: Quote) -> None: ...

    def events(self) -> AsyncIterator[VenueEvent]: ...

    def positions(self) -> Sequence[Position]: ...

    def funds(self) -> Funds: ...


@runtime_checkable
class Indicator(Protocol):
    """Technical indicator calculation protocol."""

    spec: IndicatorSpec

    def compute(
        self, values: Sequence[Any] | Mapping[str, Sequence[Any]]
    ) -> Sequence[float | None] | Mapping[str, Sequence[float | None]]: ...

    def new_state(self) -> object: ...

    def update(self, state: object, value: Any) -> float | Mapping[str, float | None] | None: ...


@runtime_checkable
class Strategy(Protocol):
    """Trading strategy protocol."""

    params_schema: type[Any]

    def on_start(self, ctx: StrategyContext) -> None: ...

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> Sequence[Intent]: ...

    def on_tick(self, ctx: StrategyContext, tick: Tick) -> Sequence[Intent]: ...

    def on_fill(self, ctx: StrategyContext, fill: Fill) -> Sequence[Intent]: ...

    def on_timer(self, ctx: StrategyContext, name: str) -> Sequence[Intent]: ...

    def on_stop(self, ctx: StrategyContext) -> None: ...

    def state(self) -> Mapping[str, object]: ...


@runtime_checkable
class RiskRule(Protocol):
    """Pre-trade risk verification rule."""

    def check(self, intent: Intent, view: RiskView) -> RiskDecision: ...


@runtime_checkable
class Notifier(Protocol):
    """Outbound messaging notifier protocol."""

    def send(self, message: Message) -> None: ...


@runtime_checkable
class Calendar(Protocol):
    """Market calendar protocol."""

    def is_trading_day(self, d: date, segment: str = "") -> bool: ...

    def sessions(self, d: date, segment: str = "") -> Sequence[TradingSession]: ...

    def next_session(self, ts: datetime, segment: str = "") -> TradingSession: ...


@runtime_checkable
class SecretsProvider(Protocol):
    """Secrets vault provider protocol."""

    def get(self, name: str) -> str: ...

    def set(self, name: str, value: str) -> None: ...

    def names(self) -> Sequence[str]: ...


@runtime_checkable
class ReferenceSource(Protocol):
    """Independent reference data source (e.g. corporate actions)."""

    def corporate_actions(
        self,
        symbol_or_isin: str,
        start: date | None = None,
        end: date | None = None,
    ) -> Sequence[ActionRecord]: ...
