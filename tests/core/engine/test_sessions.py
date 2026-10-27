"""Tests for SessionManager: lifecycle, holidays, multi-market, MCX morning-closed."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from plugins.calendars.exchange.plugin import ExchangeCalendarPlugin

from algodesk.api.errors import SafetyViolation
from algodesk.api.kinds import API_VERSION, PluginKind
from algodesk.api.manifest import PluginManifest
from algodesk.engine.clock import SimClock
from algodesk.engine.scheduler import Scheduler
from algodesk.engine.sessions import MarketState, SessionManager

IST = ZoneInfo("Asia/Kolkata")


def _get_calendars() -> dict[str, Any]:
    manifest = PluginManifest(
        name="exchange",
        version="0.1.0",
        kind=PluginKind.CALENDAR,
        api_version=API_VERSION,
        entry="plugin:ExchangeCalendarPlugin",
        capabilities={"trading_days": True},
    )
    nse = ExchangeCalendarPlugin(manifest, {"exchange": "nse"}, None)
    mcx = ExchangeCalendarPlugin(manifest, {"exchange": "mcx"}, None)
    return {"NSE": nse, "MCX": mcx}


def test_session_manager_market_key_and_state_transitions() -> None:
    cals = _get_calendars()
    sm = SessionManager(cals)

    key = sm.market_key("NSE:EQ:INFY")
    assert key == "NSE:EQ"
    assert sm.state(key) == MarketState.STOPPED

    sm.transition(key, MarketState.STARTING)
    assert sm.state(key) == MarketState.STARTING

    sm.transition(key, MarketState.RUNNING)
    assert sm.state(key) == MarketState.RUNNING

    sm.transition(key, MarketState.CLOSING)
    assert sm.state(key) == MarketState.CLOSING

    sm.transition(key, MarketState.REPORTING)
    assert sm.state(key) == MarketState.REPORTING

    sm.transition(key, MarketState.STOPPED)
    assert sm.state(key) == MarketState.STOPPED

    # Illegal transition: STOPPED -> RUNNING raises SafetyViolation
    with pytest.raises(SafetyViolation, match="Illegal market transition"):
        sm.transition(key, MarketState.RUNNING)


def test_session_manager_halt() -> None:
    cals = _get_calendars()
    sm = SessionManager(cals)
    sm.transition("NSE:EQ", MarketState.STARTING)
    sm.transition("NSE:EQ", MarketState.RUNNING)

    sm.halt("NSE:EQ", reason="Risk breach")
    assert sm.state("NSE:EQ") == MarketState.HALTED

    # Trading not allowed when halted
    now = datetime(2026, 10, 26, 9, 30, 0, tzinfo=IST).astimezone(UTC)
    assert sm.trading_allowed("NSE:EQ:INFY", now) is False


def test_session_manager_holiday_detection() -> None:
    cals = _get_calendars()
    sm = SessionManager(cals)

    # 2026-01-26 is Republic Day (full holiday for NSE and MCX)
    republic_day_ts = datetime(2026, 1, 26, 10, 0, 0, tzinfo=IST).astimezone(UTC)
    assert sm.session_for("NSE:EQ:INFY", republic_day_ts) is None
    assert sm.trading_allowed("NSE:EQ:INFY", republic_day_ts) is False

    assert sm.session_for("MCX:COM:GOLD", republic_day_ts) is None
    assert sm.trading_allowed("MCX:COM:GOLD", republic_day_ts) is False


def test_session_manager_mcx_morning_closed_day() -> None:
    """On 2026-03-03 (Holi), MCX morning session is closed, but evening session is open."""
    cals = _get_calendars()
    clock = SimClock(datetime(2026, 3, 3, 0, 0, 0, tzinfo=UTC))
    scheduler = Scheduler(clock)
    sm = SessionManager(cals, scheduler=scheduler, clock=clock)

    # Morning time at 11:00 AM IST -> no session
    morning_ts = datetime(2026, 3, 3, 11, 0, 0, tzinfo=IST).astimezone(UTC)
    session_morning = sm.session_for("MCX:COM:GOLD", morning_ts)
    assert session_morning is None

    # Evening time at 18:00 IST -> evening session exists and is tradable
    evening_ts = datetime(2026, 3, 3, 18, 0, 0, tzinfo=IST).astimezone(UTC)
    session_evening = sm.session_for("MCX:COM:GOLD", evening_ts)
    assert session_evening is not None
    assert session_evening.name == "evening"
    assert session_evening.tradable is True

    # Transition to RUNNING and verify trading_allowed
    sm.transition("MCX:COM", MarketState.STARTING)
    sm.transition("MCX:COM", MarketState.RUNNING)
    assert sm.trading_allowed("MCX:COM:GOLD", evening_ts) is True
    # Still False in the morning
    assert sm.trading_allowed("MCX:COM:GOLD", morning_ts) is False


def test_session_manager_parallel_markets() -> None:
    cals = _get_calendars()
    clock = SimClock(datetime(2026, 10, 26, 0, 0, 0, tzinfo=UTC))
    scheduler = Scheduler(clock)
    sm = SessionManager(cals, scheduler=scheduler, clock=clock)

    sm.transition("NSE:EQ", MarketState.STARTING)
    sm.transition("MCX:COM", MarketState.STARTING)

    # Schedule timers for 2026-10-26
    d = date(2026, 10, 26)
    sm.schedule_market_timers(d, scheduler)

    # Advance to 09:15:00 IST (03:45:00 UTC) -> NSE:EQ opens
    nse_open_utc = datetime(2026, 10, 26, 9, 15, 0, tzinfo=IST).astimezone(UTC)
    scheduler.advance_to(nse_open_utc)
    assert sm.state("NSE:EQ") == MarketState.RUNNING

    # Pre-open at 09:05:00 IST on NSE is NOT tradable
    preopen_utc = datetime(2026, 10, 26, 9, 5, 0, tzinfo=IST).astimezone(UTC)
    assert sm.trading_allowed("NSE:EQ:INFY", preopen_utc) is False

    # Regular session at 09:30:00 IST is tradable
    trading_utc = datetime(2026, 10, 26, 9, 30, 0, tzinfo=IST).astimezone(UTC)
    assert sm.trading_allowed("NSE:EQ:INFY", trading_utc) is True
