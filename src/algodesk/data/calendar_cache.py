"""In-memory per-run cache wrapper for Calendar protocol implementations."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from algodesk.api.protocols import Calendar, TradingSession


class CachedCalendar(Calendar):
    """Wraps any Calendar with thread-safe / fast per-run memoization."""

    def __init__(self, calendar: Calendar) -> None:
        self._calendar = calendar
        self._is_trading_cache: dict[tuple[date, str], bool] = {}
        self._sessions_cache: dict[tuple[date, str], tuple[TradingSession, ...]] = {}

    @classmethod
    def wrap(cls, calendar: Calendar | None) -> Calendar | None:
        if calendar is None or isinstance(calendar, cls):
            return calendar
        return cls(calendar)

    @property
    def underlying(self) -> Calendar:
        return self._calendar

    def coverage(self) -> tuple[date, date]:
        cov_fn = getattr(self._calendar, "coverage", None)
        if callable(cov_fn):
            res = cov_fn()
            if isinstance(res, tuple) and len(res) == 2:
                return (res[0], res[1])
        return (date.min, date.max)

    def is_trading_day(self, d: date | str, segment: str = "") -> bool:
        dt = date.fromisoformat(d) if isinstance(d, str) else d
        key = (dt, segment)
        if key in self._is_trading_cache:
            return self._is_trading_cache[key]
        try:
            res = bool(self._calendar.is_trading_day(dt, segment=segment))
        except TypeError:
            res = bool(self._calendar.is_trading_day(dt))
        self._is_trading_cache[key] = res
        return res

    def sessions(self, d: date | str, segment: str = "") -> Sequence[TradingSession]:
        dt = date.fromisoformat(d) if isinstance(d, str) else d
        key = (dt, segment)
        if key in self._sessions_cache:
            return self._sessions_cache[key]
        try:
            s_list = tuple(self._calendar.sessions(dt, segment=segment))
        except TypeError:
            try:
                s_list = tuple(self._calendar.sessions(dt))
            except Exception:
                s_list = ()
        except Exception:
            s_list = ()
        self._sessions_cache[key] = s_list
        return s_list

    def next_session(self, ts: datetime, segment: str = "") -> TradingSession:
        return self._calendar.next_session(ts, segment=segment)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._calendar, name)
