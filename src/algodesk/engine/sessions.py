"""Market session manager and per-market lifecycle state machine.

Tracks parallel market states: STARTING -> RUNNING -> CLOSING -> REPORTING -> STOPPED (or HALTED).
Determines session_for(instrument, ts) and trading_allowed(instrument, ts)
using exchange calendars.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from algodesk.api.errors import SafetyViolation
from algodesk.api.market import InstrumentId, _validate_utc_ts, parse_id
from algodesk.api.protocols import Calendar, Clock, TradingSession
from algodesk.engine.scheduler import Scheduler

IST = ZoneInfo("Asia/Kolkata")


class MarketState(StrEnum):
    """Lifecycle state of an individual exchange or market segment."""

    STARTING = "STARTING"
    RUNNING = "RUNNING"
    CLOSING = "CLOSING"
    REPORTING = "REPORTING"
    STOPPED = "STOPPED"
    HALTED = "HALTED"


_VALID_TRANSITIONS: dict[MarketState, set[MarketState]] = {
    MarketState.STARTING: {MarketState.RUNNING, MarketState.HALTED, MarketState.STOPPED},
    MarketState.RUNNING: {MarketState.CLOSING, MarketState.HALTED},
    MarketState.CLOSING: {MarketState.REPORTING, MarketState.HALTED},
    MarketState.REPORTING: {MarketState.STOPPED, MarketState.HALTED},
    MarketState.STOPPED: {MarketState.STARTING, MarketState.HALTED},
    MarketState.HALTED: {MarketState.STARTING, MarketState.STOPPED},
}


class SessionManager:
    """Multi-market session and lifecycle coordinator over exchange calendars."""

    __slots__ = ("_calendars", "_clock", "_market_states", "_scheduler")

    def __init__(
        self,
        calendars: Mapping[str, Calendar],
        scheduler: Scheduler | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._calendars = {k.upper(): v for k, v in calendars.items()}
        self._scheduler = scheduler
        self._clock = clock
        self._market_states: dict[str, MarketState] = {}

    def market_key(self, instrument: InstrumentId | str) -> str:
        """Derive standard uppercase market key e.g. 'NSE:EQ'."""
        parts = parse_id(instrument)
        return f"{parts.exchange}:{parts.segment}".upper()

    def state(self, market: str) -> MarketState:
        """Return current lifecycle state of a market (defaults to STOPPED)."""
        return self._market_states.get(market.upper(), MarketState.STOPPED)

    def transition(self, market: str, new_state: MarketState, reason: str = "") -> None:
        """Transition market state following the legal state machine."""
        key = market.upper()
        current = self.state(key)
        if current == new_state:
            return
        allowed = _VALID_TRANSITIONS.get(current, set())
        if new_state not in allowed:
            msg = (
                f"Illegal market transition for {key}: cannot move "
                f"from {current} to {new_state} ({reason or 'no reason'})"
            )
            raise SafetyViolation(
                what=msg,
                where=f"SessionManager.transition({key})",
                fix="Transition: STARTING -> RUNNING -> CLOSING -> REPORTING -> STOPPED",
            )
        self._market_states[key] = new_state

    def halt(self, market: str | None = None, reason: str = "") -> None:
        """Halt a single market or all configured markets."""
        if market:
            self._market_states[market.upper()] = MarketState.HALTED
        else:
            for k in list(self._market_states.keys()):
                self._market_states[k] = MarketState.HALTED

    def session_for(
        self,
        instrument: InstrumentId | str,
        ts: datetime,
    ) -> TradingSession | None:
        """Find the matching TradingSession for instrument at ts."""
        _validate_utc_ts(ts, "ts")
        parts = parse_id(instrument)
        cal = self._calendars.get(parts.exchange.upper())
        if cal is None:
            return None

        ist_dt = ts.astimezone(IST)
        trading_date = ist_dt.date()
        if not cal.is_trading_day(trading_date, segment=parts.segment.lower()):
            return None

        sessions = cal.sessions(trading_date, segment=parts.segment.lower())
        for s in sessions:
            if s.start <= ts < s.end:
                return s
        return None

    def trading_allowed(
        self,
        instrument: InstrumentId | str,
        ts: datetime,
    ) -> bool:
        """Return True if trading is active and permitted for instrument at ts."""
        session = self.session_for(instrument, ts)
        if session is None or not session.tradable:
            return False
        m_state = self.state(self.market_key(instrument))
        return m_state == MarketState.RUNNING

    def schedule_market_timers(
        self,
        d: date,
        scheduler: Scheduler,
    ) -> None:
        """Schedule start, open, and close timers across all configured markets."""
        for exch, cal in self._calendars.items():
            segments = ["eq", "fno", "cds"] if exch in ("NSE", "BSE") else ["com"]
            for seg in segments:
                if not cal.is_trading_day(d, segment=seg):
                    continue
                sessions = cal.sessions(d, segment=seg)
                if not sessions:
                    continue
                market = f"{exch}:{seg}".upper()
                if self.state(market) == MarketState.STOPPED:
                    self.transition(market, MarketState.STARTING, "market schedule")

                for s in sessions:
                    if not s.tradable:
                        continue

                    def _make_open_cb(m: str) -> Callable[..., None]:
                        def _open(*_: Any) -> None:
                            self.transition(m, MarketState.RUNNING, "open timer")

                        return _open

                    def _make_close_cb(m: str) -> Callable[..., None]:
                        def _close(*_: Any) -> None:
                            self.transition(m, MarketState.CLOSING, "close timer")
                            self.transition(m, MarketState.REPORTING, "session end")
                            self.transition(m, MarketState.STOPPED, "reporting done")

                        return _close

                    open_name = f"session.{market}.{s.name}.open"
                    close_name = f"session.{market}.{s.name}.close"
                    scheduler.schedule_at(open_name, s.start, _make_open_cb(market))
                    scheduler.schedule_at(close_name, s.end, _make_close_cb(market))
