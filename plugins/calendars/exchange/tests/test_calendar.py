"""Unit tests for ExchangeCalendarPlugin."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from algodesk.api.errors import CalendarCoverageError
from algodesk.api.manifest import PluginManifest

from ..plugin import ExchangeCalendarPlugin


class DummyParams:
    def __init__(self, overrides: dict[str, Any] | None = None) -> None:
        self.overrides = overrides or {}

    def get(self, name: str, scope: str | None = None) -> Any:
        return self.overrides.get(name)


class DummyCtx:
    def __init__(self, params: Any = None) -> None:
        self.params = params


def _make_plugin(exchange: str, params: Any = None) -> ExchangeCalendarPlugin:
    manifest = PluginManifest(
        name="exchange",
        kind="calendar",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="plugin:ExchangeCalendarPlugin",
        capabilities={"holidays": True, "sessions": True, "special_sessions": True},
    )
    ctx = DummyCtx(params)
    return ExchangeCalendarPlugin(manifest, {"exchange": exchange}, ctx)


def test_mcx_trading_day_and_partial_closures() -> None:
    cal = _make_plugin("mcx")

    # Regular trading day (Wednesday 2026-10-21)
    assert cal.is_trading_day(date(2026, 10, 21), segment="com") is True

    # Morning-only closure: 2026-10-20 (Dussera) is still a trading day because evening is open!
    assert cal.is_trading_day(date(2026, 10, 20), segment="com") is True

    # Full closure: 2026-10-02 (Mahatma Gandhi Jayanti, closed = all)
    assert cal.is_trading_day(date(2026, 10, 2), segment="com") is False

    # Weekend (Sunday 2026-10-25)
    assert cal.is_trading_day(date(2026, 10, 25), segment="com") is False

    # Outside coverage raises CalendarCoverageError
    with pytest.raises(CalendarCoverageError):
        cal.is_trading_day(date(2025, 12, 31), segment="com")
    with pytest.raises(CalendarCoverageError):
        cal.is_trading_day(date(2027, 1, 1), segment="com")


def test_mcx_sessions_morning_and_evening() -> None:
    cal = _make_plugin("mcx")

    # 2026-10-22 (Thursday, normal day) -> 2 sessions (morning, evening)
    s_22 = cal.sessions(date(2026, 10, 22), segment="com")
    assert len(s_22) == 2
    assert s_22[0].name == "morning"
    assert s_22[1].name == "evening"

    # Morning session: 09:00 IST -> 03:30 UTC, 17:00 IST -> 11:30 UTC
    assert s_22[0].start == datetime(2026, 10, 22, 3, 30, tzinfo=UTC)
    assert s_22[0].end == datetime(2026, 10, 22, 11, 30, tzinfo=UTC)

    # Evening session: 17:00 IST -> 11:30 UTC, 23:30 IST -> 18:00 UTC
    assert s_22[1].start == datetime(2026, 10, 22, 11, 30, tzinfo=UTC)
    assert s_22[1].end == datetime(2026, 10, 22, 18, 0, tzinfo=UTC)

    # 2026-10-20 (Dussera, morning closed) -> evening ONLY!
    s_20 = cal.sessions(date(2026, 10, 20), segment="com")
    assert len(s_20) == 1
    assert s_20[0].name == "evening"
    assert s_20[0].start == datetime(2026, 10, 20, 11, 30, tzinfo=UTC)
    assert s_20[0].end == datetime(2026, 10, 20, 18, 0, tzinfo=UTC)


def test_nse_sessions_and_holidays() -> None:
    cal = _make_plugin("nse")

    # 16 declared holidays
    holidays = cal.holidays(year=2026)
    assert len(holidays) == 16

    # Normal day (2026-10-23)
    sessions = cal.sessions(date(2026, 10, 23), segment="eq")
    assert len(sessions) == 2
    assert sessions[0].name == "preopen"
    assert sessions[1].name == "regular"

    # Pre-open 09:00 IST -> 03:30 UTC
    assert sessions[0].start == datetime(2026, 10, 23, 3, 30, tzinfo=UTC)
    assert sessions[0].end == datetime(2026, 10, 23, 3, 45, tzinfo=UTC)

    # Regular 09:15 IST -> 03:45 UTC, 15:30 IST -> 10:00 UTC
    assert sessions[1].start == datetime(2026, 10, 23, 3, 45, tzinfo=UTC)
    assert sessions[1].end == datetime(2026, 10, 23, 10, 0, tzinfo=UTC)


def test_special_session_timings_pending() -> None:
    cal = _make_plugin("nse")

    # 2026-11-08 has Diwali Muhurat special session with timings_pending = true
    pending = cal.pending_special_sessions()
    assert any(p["date"] == date(2026, 11, 8) for p in pending)

    with pytest.raises(CalendarCoverageError, match="Timings pending for special session"):
        cal.is_trading_day(date(2026, 11, 8), segment="eq")

    with pytest.raises(CalendarCoverageError, match="Timings pending for special session"):
        cal.sessions(date(2026, 11, 8), segment="eq")


def test_dynamic_param_change_effect() -> None:
    params = DummyParams({"market.nse.eq.regular.close": "16:00"})
    cal = _make_plugin("nse", params=params)

    s1 = cal.sessions(date(2026, 10, 23), segment="eq")
    assert s1[1].end == datetime(2026, 10, 23, 10, 30, tzinfo=UTC)  # 16:00 IST = 10:30 UTC

    # Dynamic change in params
    params.overrides["market.nse.eq.regular.close"] = "15:00"
    s2 = cal.sessions(date(2026, 10, 23), segment="eq")
    assert s2[1].end == datetime(2026, 10, 23, 9, 30, tzinfo=UTC)  # 15:00 IST = 09:30 UTC


def test_next_session() -> None:
    cal = _make_plugin("mcx")

    # Time before morning session on 2026-10-22
    t_before = datetime(2026, 10, 22, 2, 0, tzinfo=UTC)
    next_s = cal.next_session(t_before, segment="com")
    assert next_s.name == "morning"
    assert next_s.start == datetime(2026, 10, 22, 3, 30, tzinfo=UTC)

    # Time during morning session
    t_mid = datetime(2026, 10, 22, 5, 0, tzinfo=UTC)
    next_s2 = cal.next_session(t_mid, segment="com")
    assert next_s2.name == "morning"

    # Time after morning session, before evening session
    t_after_m = datetime(2026, 10, 22, 11, 35, tzinfo=UTC)
    next_s3 = cal.next_session(t_after_m, segment="com")
    assert next_s3.name == "evening"
