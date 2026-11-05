"""Tests for M2-3b CP3: Store, ContractResolver, universes, feasibility."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from algodesk.api.errors import DataError
from algodesk.api.market import Instrument, InstrumentId
from algodesk.data.contracts import ContractResolver
from algodesk.data.groups import resolve_group_universe
from algodesk.data.instruments_store import InstrumentStore

# --- 1. InstrumentStore tests ---


def test_instrument_store_diff(tmp_path: Path) -> None:
    store = InstrumentStore(tmp_path)
    d1 = date(2026, 11, 1)
    d2 = date(2026, 11, 2)

    # Day 1 instruments
    data1 = [
        {
            "exchange": "NSE",
            "segment": "EQ",
            "symbol": "RELIANCE",
            "trading_symbol": "RELIANCE",
            "lot_size": 1,
            "tick_size": "0.05",
            "isin": "INE002A01018",
        },
        {
            "exchange": "NSE",
            "segment": "FUT",
            "symbol": "VEDL",
            "trading_symbol": "VEDL FUT 29 DEC 26",
            "underlying_symbol": "VEDL",
            "expiry": "2026-12-29",
            "lot_size": 1150,
            "tick_size": "0.05",
            "instrument_type": "FUT",
        },
        {
            "exchange": "NSE",
            "segment": "EQ",
            "symbol": "OLD_STOCK",
            "trading_symbol": "OLD_STOCK",
            "lot_size": 1,
            "tick_size": "0.05",
        },
    ]

    # Day 2 instruments: OLD_STOCK removed, NEW_STOCK added, VEDL lot size changed from 1150 to 1200
    data2 = [
        {
            "exchange": "NSE",
            "segment": "EQ",
            "symbol": "RELIANCE",
            "trading_symbol": "RELIANCE",
            "lot_size": 1,
            "tick_size": "0.05",
            "isin": "INE002A01018",
        },
        {
            "exchange": "NSE",
            "segment": "FUT",
            "symbol": "VEDL",
            "trading_symbol": "VEDL FUT 29 DEC 26",
            "underlying_symbol": "VEDL",
            "expiry": "2026-12-29",
            "lot_size": 1200,  # Changed lot size!
            "tick_size": "0.05",
            "instrument_type": "FUT",
        },
        {
            "exchange": "NSE",
            "segment": "EQ",
            "symbol": "NEW_STOCK",
            "trading_symbol": "NEW_STOCK",
            "lot_size": 1,
            "tick_size": "0.05",
        },
    ]

    store.save_master("upstox", d1, json.dumps(data1).encode("utf-8"))
    store.save_master("upstox", d2, json.dumps(data2).encode("utf-8"))

    journal_mock = MagicMock()
    report = store.diff("upstox", d1, d2, journal=journal_mock)

    assert report.has_changes
    assert len(report.added) == 1
    assert report.added[0].id == InstrumentId("NSE:EQ:NEW_STOCK")

    assert len(report.removed) == 1
    assert report.removed[0].id == InstrumentId("NSE:EQ:OLD_STOCK")

    assert len(report.changed) == 1
    change = report.changed[0]
    assert change.id == InstrumentId("NSE:FUT:VEDL:2026-12-29")
    assert "lot_size" in change.changes
    assert change.changes["lot_size"] == (1150, 1200)

    # Verify journaled event
    journal_mock.append.assert_called_once()
    event_name, payload = journal_mock.append.call_args[0]
    assert event_name == "instruments.diff"
    assert payload["added_count"] == 1
    assert payload["removed_count"] == 1
    assert payload["changed_count"] == 1


# --- 2. ContractResolver tests ---


def test_contract_resolver_near_and_next_and_rollover() -> None:
    fut_nov = Instrument(
        exchange="MCX",
        segment="FUT",
        symbol="GOLD",
        expiry=date(2026, 11, 5),
        lot_size=1,
        tick_size=Decimal("100"),
    )
    fut_dec = Instrument(
        exchange="MCX",
        segment="FUT",
        symbol="GOLD",
        expiry=date(2026, 12, 4),
        lot_size=1,
        tick_size=Decimal("100"),
    )
    instruments = [fut_nov, fut_dec]

    # As of 2026-10-20 (well before rollover): "near" yields fut_nov, "next" yields fut_dec
    resolver = ContractResolver(instruments, params={"contract.rollover_days": 3})
    res_near = resolver.resolve_contract("GOLD", "future", "near", as_of=date(2026, 10, 20))
    assert res_near.id == fut_nov.id

    res_next = resolver.resolve_contract("GOLD", "future", "next", as_of=date(2026, 10, 20))
    assert res_next.id == fut_dec.id

    # As of 2026-11-03 (within 3 calendar days of 2026-11-05): "near" rolls over to fut_dec!
    res_rolled = resolver.resolve_contract("GOLD", "future", "near", as_of=date(2026, 11, 3))
    assert res_rolled.id == fut_dec.id


def test_contract_resolver_holiday_expiry_and_calendar_failure() -> None:
    # Nov 8, 2026 is a Sunday (holiday).
    sunday_exp = date(2026, 11, 8)
    fut = Instrument(
        exchange="NSE",
        segment="FUT",
        symbol="NIFTY",
        expiry=sunday_exp,
        lot_size=50,
        tick_size=Decimal("0.05"),
    )

    calendar_mock = MagicMock(spec=["is_trading_day"])
    # sunday_exp is not trading day, friday_exp is
    calendar_mock.is_trading_day.side_effect = lambda d: d != sunday_exp

    resolver = ContractResolver([fut], calendar=calendar_mock)
    res = resolver.resolve_contract("NIFTY", "future", "near", as_of=date(2026, 11, 1))
    assert res.id == fut.id

    # If calendar raises an error / cannot determine: fail closed
    bad_calendar = MagicMock(spec=["is_trading_day"])
    bad_calendar.is_trading_day.side_effect = RuntimeError("Calendar DB unavailable")
    bad_resolver = ContractResolver([fut], calendar=bad_calendar)
    with pytest.raises(DataError, match="Calendar coverage check failed"):
        bad_resolver.resolve_contract("NIFTY", "future", "near", as_of=date(2026, 11, 1))


def test_contract_resolver_options() -> None:
    opt_ce = Instrument(
        exchange="NSE",
        segment="OPT",
        symbol="NIFTY",
        expiry=date(2026, 10, 29),
        strike=Decimal("24000"),
        option_type="CE",
        lot_size=50,
        tick_size=Decimal("0.05"),
    )
    opt_pe = Instrument(
        exchange="NSE",
        segment="OPT",
        symbol="NIFTY",
        expiry=date(2026, 10, 29),
        strike=Decimal("24000"),
        option_type="PE",
        lot_size=50,
        tick_size=Decimal("0.05"),
    )

    resolver = ContractResolver([opt_ce, opt_pe])
    matched_ce = resolver.resolve_contract(
        "NIFTY",
        "option",
        {"strike": Decimal("24000"), "type": "CE", "expiry": "near"},
        as_of=date(2026, 10, 20),
    )
    assert matched_ce.id == opt_ce.id

    matched_pe = resolver.resolve_contract(
        "NIFTY",
        "option",
        {"strike": Decimal("24000"), "type": "PE", "expiry": "near"},
        as_of=date(2026, 10, 20),
    )
    assert matched_pe.id == opt_pe.id


# --- 3. Frozen Universes tests ---


def test_frozen_universes_deterministic_hash(tmp_path: Path) -> None:
    groups_dir = tmp_path / "groups"
    groups_dir.mkdir()
    group_file = groups_dir / "test_univ.toml"
    group_file.write_text(
        """
name = "test_univ"
description = "Test universe"
members = [
    "NSE:EQ:RELIANCE",
    "MCX:FNO:GOLD FUT 04 DEC 26",
    "NSE:EQ:TCS"
]
"""
    )

    members, h1 = resolve_group_universe("test_univ", groups_dir=groups_dir)
    assert members == [
        "MCX:FUT:GOLD:2026-12-04",
        "NSE:EQ:RELIANCE",
        "NSE:EQ:TCS",
    ]

    # Verify deterministic hash
    _, h2 = resolve_group_universe("test_univ", groups_dir=groups_dir)
    assert h1 == h2
    assert len(h1) == 16
