"""StrategyContext implementation providing read-only access to runtime services.

Exposes clock, instruments, historical bar access, indicators, position queries,
portfolio snapshots, validated strategy parameters, and risk state.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Any

from algodesk.api.market import Bar, Instrument, InstrumentId
from algodesk.api.protocols import Clock, StrategyContext
from algodesk.api.trading import Position
from algodesk.engine.indicator_manager import IndicatorManager


class BarBuffer:
    """Thread-safe rolling buffer of completed bars for strategy consumption."""

    def __init__(self, max_len: int | None = None) -> None:
        self._max_len = max_len
        self._bars: dict[tuple[InstrumentId, str], list[Bar]] = {}

    def append(self, bar: Bar) -> None:
        """Store a bar if complete, discarding look-ahead/incomplete bars."""
        if not getattr(bar, "complete", True):
            return
        key = (bar.instrument, bar.timeframe)
        lst = self._bars.setdefault(key, [])
        lst.append(bar)
        if self._max_len is not None and len(lst) > self._max_len:
            del lst[: len(lst) - self._max_len]

    def get(
        self,
        instrument: InstrumentId | str,
        timeframe: str,
        n: int = 1,
    ) -> Sequence[Bar]:
        """Return the last n completed bars in chronological order."""
        inst_id = InstrumentId(str(instrument))
        lst = self._bars.get((inst_id, timeframe), [])
        if n <= 0 or not lst:
            return ()
        return tuple(lst[-n:])


def build_strategy_context(
    clock: Clock,
    instruments: Mapping[InstrumentId | str, Instrument],
    indicator_manager: IndicatorManager,
    bar_buffer: BarBuffer,
    position_fn: Callable[[InstrumentId | str], Position | None] | None = None,
    portfolio_fn: Callable[[], Any] | Any = None,
    params: Mapping[str, Any] | None = None,
    risk_state: Mapping[str, Any] | None = None,
    logger: logging.Logger | Any | None = None,
) -> StrategyContext:
    """Build a StrategyContext with read-only mappings and callable accessors."""
    inst_dict: dict[InstrumentId, Instrument] = {
        InstrumentId(str(k)): v for k, v in instruments.items()
    }
    readonly_instruments: Mapping[InstrumentId, Instrument] = MappingProxyType(inst_dict)
    readonly_params: Mapping[str, Any] = MappingProxyType(dict(params or {}))
    readonly_risk_state: Mapping[str, Any] = MappingProxyType(dict(risk_state or {}))

    def _get_bars(
        instrument: InstrumentId | str,
        timeframe: str,
        n: int = 1,
    ) -> Sequence[Bar]:
        return bar_buffer.get(instrument, timeframe, n)

    def _get_indicator(
        name: str,
        instrument: InstrumentId | str,
        timeframe: str,
        **kw: Any,
    ) -> float | None:
        return indicator_manager.get(name, instrument, timeframe, **kw)

    def _get_position(instrument: InstrumentId | str) -> Position | None:
        if position_fn is not None:
            return position_fn(instrument)
        return None

    portfolio_obj = portfolio_fn() if callable(portfolio_fn) else portfolio_fn

    log_inst = logger or logging.getLogger("algodesk.strategy")

    return StrategyContext(
        clock=clock,
        instruments=readonly_instruments,
        bars=_get_bars,
        indicator=_get_indicator,
        position=_get_position,
        portfolio=portfolio_obj,
        params=readonly_params,
        risk_state=readonly_risk_state,
        log=log_inst,
    )
