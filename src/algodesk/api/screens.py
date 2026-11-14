"""Screen types, protocols, and result contracts for AlgoDesk Next."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from algodesk.api.market import Bar, InstrumentId


class SkipReason(StrEnum):
    """Reason why an instrument could not be evaluated by a screen."""

    NO_DATA = "no_data"
    INSUFFICIENT_HISTORY = "insufficient_history"
    ADJUSTMENT_REFUSED = "adjustment_refused"
    CALENDAR_GAP = "calendar_gap"
    INDICATOR_ERROR = "indicator_error"


@dataclass(frozen=True, slots=True)
class ScreenRow:
    """Evaluation output for a single instrument in a screen."""

    symbol: str
    passed: bool
    score: float = 0.0
    values: Mapping[str, float | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ScreenSpec:
    """Specification describing screen metadata, schemas, and outputs."""

    name: str
    description: str = ""
    timeframe: str = "1d"
    parameters_schema: type[Any] | Mapping[str, Any] = field(default_factory=dict)
    output_columns: Sequence[str] = field(default_factory=tuple)
    adjust: str = "splits"
    required_indicators: Sequence[Mapping[str, Any]] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ScreenResult:
    """Immutable result payload of a completed screen run."""

    name: str
    as_of: date
    timeframe: str = "1d"
    rows: Sequence[ScreenRow] = field(default_factory=tuple)
    skipped: Sequence[tuple[str, SkipReason, str]] = field(default_factory=tuple)
    universe_hash: str = ""
    result_hash: str = ""
    provenance: Mapping[str, Any] = field(default_factory=dict)
    parameters: Mapping[str, Any] = field(default_factory=dict)
    flags: Mapping[str, Any] = field(default_factory=dict)
    verdict: str = "PASS"
    fail_reason: str | None = None
    run_id: str = ""
    timing: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ScreenBars:
    """Historical bars and provenance returned by a ScreenDataReader."""

    bars: Sequence[Bar]
    provenance: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ScreenRefusal:
    """Refusal returned by a ScreenDataReader explaining why bars were refused."""

    reason: SkipReason
    detail: str


@runtime_checkable
class IndicatorResolver(Protocol):
    """Protocol for resolving and instantiating indicators by name."""

    def get_indicator(self, name: str, **params: Any) -> Any:
        """Create or resolve an indicator instance by name with given parameters."""
        ...


@runtime_checkable
class ScreenDataReader(Protocol):
    """Protocol for fetching historical bars with provenance for screens."""

    def get_bars(
        self,
        instrument: InstrumentId | str,
        timeframe: str,
        start: datetime,
        end: datetime,
        adjust: str = "splits",
        accept_unverified: bool = False,
    ) -> ScreenBars | ScreenRefusal:
        """Fetch bars and provenance or return a ScreenRefusal."""
        ...


@runtime_checkable
class SymbolFeatureView(Protocol):
    """Read-only view of bars and indicator values for a single symbol."""

    @property
    def symbol(self) -> str:
        """Instrument symbol."""
        ...

    @property
    def close(self) -> float | None:
        """Last completed bar close price."""
        ...

    @property
    def open(self) -> float | None:
        """Last completed bar open price."""
        ...

    @property
    def high(self) -> float | None:
        """Last completed bar high price."""
        ...

    @property
    def low(self) -> float | None:
        """Last completed bar low price."""
        ...

    @property
    def volume(self) -> float | None:
        """Last completed bar volume."""
        ...

    def get_bar(self, field: str, bars_ago: int = 0) -> float | None:
        """Get OHLCV value at bars_ago offset (0 is last completed bar)."""
        ...

    def get_indicator(
        self,
        name: str,
        output: str | None = None,
        bars_ago: int = 0,
        **params: Any,
    ) -> float | None:
        """Get indicator output value at bars_ago offset."""
        ...


@runtime_checkable
class FeatureView(Protocol):
    """Read-only view providing completed bar fields and indicator values."""

    @property
    def symbols(self) -> Sequence[str]:
        """Sequence of symbols available in this feature view."""
        ...

    def for_symbol(self, symbol: str) -> SymbolFeatureView:
        """Return symbol-scoped feature view."""
        ...

    def get_bar(self, symbol: str, field: str, bars_ago: int = 0) -> float | None:
        """Return OHLCV field value for a symbol at bars_ago."""
        ...

    def get_indicator(
        self,
        symbol: str,
        name: str,
        output: str | None = None,
        bars_ago: int = 0,
        **params: Any,
    ) -> float | None:
        """Return indicator output value for a symbol at bars_ago."""
        ...

    def universe_table(self) -> Mapping[str, Mapping[str, float | None]]:
        """Return full universe table of computed features for cross-sectional ranking."""
        ...


@runtime_checkable
class Screen(Protocol):
    """Screen plugin protocol for filtering and ranking symbols."""

    spec: ScreenSpec

    def evaluate(self, features: FeatureView) -> Sequence[ScreenRow]:
        """Evaluate screen conditions over features and return symbol rows."""
        ...
