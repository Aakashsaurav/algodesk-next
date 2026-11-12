"""Risk view aggregating portfolio state, quotes, parameters, and trading counters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from algodesk.api.market import Instrument, InstrumentId, Quote
from algodesk.api.protocols import Clock
from algodesk.api.services import ParamReader
from algodesk.api.trading import Order, Position


@dataclass(frozen=True, slots=True)
class RiskViewContext:
    """Read-only view provided to RiskRules during pre-trade evaluation."""

    portfolio: Any = None
    positions: Mapping[str, Position] = field(default_factory=dict)
    open_orders: Sequence[Order] = field(default_factory=tuple)
    daily_pnl: Decimal = Decimal("0.00")
    latest_quotes: Mapping[InstrumentId, Quote] = field(default_factory=dict)
    params: ParamReader | Mapping[str, Any] = field(default_factory=dict)
    clock: Clock | Any = None
    instruments: Mapping[InstrumentId, Instrument] = field(default_factory=dict)
    session_manager: Any = None
    orders_last_minute: int = 0
    daily_trades_count: int = 0
    profile: str = "conservative"
    allocated_capital: Decimal = Decimal("100000.00")
    strategy: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def quote_for(self, instrument: InstrumentId | str) -> Quote | None:
        """Fetch latest quote for given instrument if available."""
        return self.latest_quotes.get(InstrumentId(str(instrument)))

    def instrument_for(self, instrument: InstrumentId | str) -> Instrument | None:
        """Fetch instrument specification if known."""
        return self.instruments.get(InstrumentId(str(instrument)))
