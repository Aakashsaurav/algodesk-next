"""Exchange trading calendar plugin for Indian markets (NSE, BSE, MCX)."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from algodesk.api import CalendarCoverageError, PluginBase, TradingSession

_WEEKDAYS_MAP = ["MON,TUE,WED,THU,FRI", "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

_DEFAULT_TIMES: dict[str, dict[str, dict[str, str]]] = {
    "nse": {
        "eq": {
            "preopen.open": "09:00",
            "preopen.close": "09:15",
            "regular.open": "09:15",
            "regular.close": "15:30",
        },
        "fno": {"regular.open": "09:15", "regular.close": "15:30"},
        "cds": {"regular.open": "09:00", "regular.close": "17:00"},
    },
    "bse": {
        "eq": {
            "preopen.open": "09:00",
            "preopen.close": "09:15",
            "regular.open": "09:15",
            "regular.close": "15:30",
        },
        "fno": {"regular.open": "09:15", "regular.close": "15:30"},
        "cds": {"regular.open": "09:00", "regular.close": "17:00"},
    },
    "mcx": {
        "com": {
            "morning.open": "09:00",
            "morning.close": "17:00",
            "evening.open": "17:00",
            "evening.close": "23:30",
        },
    },
}


class ExchangeCalendarPlugin(PluginBase):
    """Calendar plugin implementation reading session parameters and holiday data."""

    def __init__(self, manifest: Any, config: Mapping[str, object] | None, ctx: Any) -> None:
        super().__init__(manifest, config or {}, ctx)
        self.exchange = str(self.config.get("exchange", "nse")).lower()
        self._data = self._load_data(self.exchange)
        self.coverage_start: date = self._data["coverage_start"]
        self.coverage_end: date = self._data["coverage_end"]
        self._tz = ZoneInfo(self._data.get("timezone", "Asia/Kolkata"))

    def _load_data(self, exchange: str) -> dict[str, Any]:
        data_file = Path(__file__).parent / "data" / f"{exchange}.toml"
        if not data_file.is_file():
            raise CalendarCoverageError(
                what=f"Calendar data file missing for exchange '{exchange}': {data_file}",
                where="ExchangeCalendarPlugin:_load_data",
                fix=f"Add data/{exchange}.toml to plugins/calendars/exchange/data/",
            )
        with open(data_file, "rb") as f:
            return tomllib.load(f)

    def coverage(self) -> tuple[date, date]:
        """Return the (start_date, end_date) coverage range."""
        return self.coverage_start, self.coverage_end

    def pending_special_sessions(self) -> list[dict[str, Any]]:
        """Return special sessions that have unannounced timings."""
        specials = self._data.get("special", [])
        return [s for s in specials if s.get("timings_pending", False)]

    def holidays(self, year: int | None = None) -> list[dict[str, Any]]:
        """Return declared holiday list, optionally filtered by year."""
        all_h = self._data.get("holiday", [])
        if year is None:
            return list(all_h)
        return [h for h in all_h if h["date"].year == year]

    def _default_segment(self) -> str:
        return "com" if self.exchange == "mcx" else "eq"

    def _get_segment_sessions(self, segment: str) -> list[str]:
        seg_info = self._data.get("segments", {}).get(segment)
        if seg_info and "sessions" in seg_info:
            return list(seg_info["sessions"])
        if self.exchange == "mcx":
            return ["morning", "evening"]
        if segment in ("fno", "cds"):
            return ["regular"]
        return ["preopen", "regular"]

    def _get_segment_tradable(self, segment: str) -> list[str]:
        seg_info = self._data.get("segments", {}).get(segment)
        if seg_info and "tradable" in seg_info:
            return list(seg_info["tradable"])
        if self.exchange == "mcx":
            return ["morning", "evening"]
        if segment in ("fno", "cds"):
            return ["regular"]
        return ["regular"]

    def _get_param(self, name: str, default: str) -> str:
        if self.ctx is not None and getattr(self.ctx, "params", None) is not None:
            try:
                val = self.ctx.params.get(name)
                if val is not None:
                    return str(val)
            except Exception:
                pass
        return default

    def _get_session_times(self, segment: str, session_name: str, d: date) -> tuple[str, str]:
        defaults = _DEFAULT_TIMES.get(self.exchange, {}).get(segment, {})
        def_open = defaults.get(f"{session_name}.open", "09:00")
        def_close = defaults.get(f"{session_name}.close", "15:30")

        open_key = f"market.{self.exchange}.{segment}.{session_name}.open"
        close_key = f"market.{self.exchange}.{segment}.{session_name}.close"
        open_val = self._get_param(open_key, def_open)
        close_val = self._get_param(close_key, def_close)

        # Check session overrides
        for override in self._data.get("session_override", []):
            is_match = (
                override.get("from") <= d <= override.get("to")
                and override.get("session") == session_name
            )
            if is_match:
                if "close" in override:
                    close_val = override["close"]
                if "open" in override:
                    open_val = override["open"]

        return open_val, close_val

    def _check_coverage(self, d: date, caller: str) -> None:
        if d < self.coverage_start or d > self.coverage_end:
            raise CalendarCoverageError(
                what=(
                    f"Date {d} outside coverage [{self.coverage_start}, {self.coverage_end}] "
                    f"for exchange '{self.exchange}'"
                ),
                where=f"ExchangeCalendarPlugin:{caller}",
                fix="Request date within coverage or update calendar data",
            )

    def is_trading_day(self, d: date, segment: str = "") -> bool:
        """Check whether d is a trading day for the given exchange and segment."""
        self._check_coverage(d, "is_trading_day")
        seg = segment or self._default_segment()

        # Check special sessions first
        for special in self._data.get("special", []):
            if special.get("date") == d:
                if special.get("timings_pending", False):
                    raise CalendarCoverageError(
                        what=f"Timings pending for special session on {d}",
                        where="ExchangeCalendarPlugin:is_trading_day",
                        fix="Wait for exchange circular announcing session timings",
                    )
                return True

        # Check weekdays
        weekdays_key = f"market.{self.exchange}.{seg}.weekdays"
        weekdays_str = self._get_param(weekdays_key, "MON,TUE,WED,THU,FRI")
        allowed_weekdays = [w.strip() for w in weekdays_str.split(",")]
        weekday_name = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][d.weekday()]
        if weekday_name not in allowed_weekdays:
            return False

        # Check holidays
        for h in self._data.get("holiday", []):
            if h.get("date") == d:
                closed = h.get("closed", ["all"])
                if "all" in closed:
                    return False
                seg_sessions = self._get_segment_sessions(seg)
                if all(s in closed for s in seg_sessions):
                    return False
                return True

        return True

    def sessions(self, d: date, segment: str = "") -> Sequence[TradingSession]:
        """Return all trading sessions on date d for segment in timezone-aware UTC."""
        self._check_coverage(d, "sessions")
        seg = segment or self._default_segment()

        # Check special sessions
        for special in self._data.get("special", []):
            if special.get("date") == d:
                if special.get("timings_pending", False):
                    raise CalendarCoverageError(
                        what=f"Timings pending for special session on {d}",
                        where="ExchangeCalendarPlugin:sessions",
                        fix="Wait for exchange circular announcing session timings",
                    )
                # If special has confirmed session times
                spec_sessions: list[TradingSession] = []
                for s_def in special.get("sessions", []):
                    s_name = s_def.get("name", "special")
                    s_open = s_def["open"]
                    s_close = s_def["close"]
                    ts_start = self._to_utc(d, s_open)
                    ts_end = self._to_utc(d, s_close)
                    is_trad = bool(s_def.get("tradable", True))
                    spec_sessions.append(
                        TradingSession(name=s_name, start=ts_start, end=ts_end, tradable=is_trad)
                    )
                return spec_sessions

        # Check weekdays
        weekdays_key = f"market.{self.exchange}.{seg}.weekdays"
        weekdays_str = self._get_param(weekdays_key, "MON,TUE,WED,THU,FRI")
        allowed_weekdays = [w.strip() for w in weekdays_str.split(",")]
        weekday_name = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][d.weekday()]
        if weekday_name not in allowed_weekdays:
            return []

        # Check holidays and closed sessions
        closed_sessions: list[str] = []
        for h in self._data.get("holiday", []):
            if h.get("date") == d:
                closed = h.get("closed", ["all"])
                if "all" in closed:
                    return []
                closed_sessions.extend(closed)

        results: list[TradingSession] = []
        tradable_names = self._get_segment_tradable(seg)
        for s_name in self._get_segment_sessions(seg):
            if s_name in closed_sessions:
                continue
            open_str, close_str = self._get_session_times(seg, s_name, d)
            ts_start = self._to_utc(d, open_str)
            ts_end = self._to_utc(d, close_str)
            is_tradable = s_name in tradable_names
            results.append(
                TradingSession(name=s_name, start=ts_start, end=ts_end, tradable=is_tradable)
            )

        return results

    def _to_utc(self, d: date, time_str: str) -> datetime:
        parts = time_str.split(":")
        h, m = int(parts[0]), int(parts[1])
        local_dt = datetime(d.year, d.month, d.day, h, m, tzinfo=self._tz)
        return local_dt.astimezone(UTC)

    def next_session(self, ts: datetime, segment: str = "") -> TradingSession:
        """Find the earliest trading session ending after ts."""
        if ts.tzinfo is None:
            utc_ts = ts.replace(tzinfo=UTC)
        else:
            utc_ts = ts.astimezone(UTC)

        local_date = utc_ts.astimezone(self._tz).date()
        seg = segment or self._default_segment()

        curr_d = max(local_date, self.coverage_start)
        while curr_d <= self.coverage_end:
            try:
                for sess in self.sessions(curr_d, seg):
                    if sess.end > utc_ts:
                        return sess
            except CalendarCoverageError:
                pass
            curr_d += timedelta(days=1)

        raise CalendarCoverageError(
            what=(
                f"No upcoming session found within coverage "
                f"[{self.coverage_start}, {self.coverage_end}]"
            ),
            where="ExchangeCalendarPlugin:next_session",
            fix="Extend calendar data coverage",
        )
