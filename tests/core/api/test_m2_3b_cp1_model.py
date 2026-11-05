"""Tests for M2-3b CP1: Instrument fields, id round-trip, mapping, and aliases."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from hypothesis import given
from hypothesis import strategies as st
from plugins.brokers.upstox.instruments import UpstoxInstrumentManager
from plugins.brokers.yfinance.config_schema import YFinanceConfig
from plugins.brokers.yfinance.plugin import YFinanceBrokerPlugin

from algodesk.api.errors import DataError, NotSupported
from algodesk.api.market import (
    IdParts,
    Instrument,
    InstrumentId,
    format_id,
    parse_id,
)
from algodesk.data.aliases import register_alias, resolve_alias

# --- 1. Property tests for round-trip ---

valid_exchanges = st.sampled_from(["NSE", "BSE", "MCX", "NFO", "BFO"])
valid_segments = st.sampled_from(["EQ", "FUT", "OPT", "INDEX", "COM"])
valid_symbols = st.from_regex(r"^[A-Z0-9]{1,10}$", fullmatch=True)
valid_dates = st.dates(min_value=date(2020, 1, 1), max_value=date(2035, 12, 31))
valid_strikes = st.decimals(min_value=Decimal("1"), max_value=Decimal("100000"), places=2)
valid_option_types = st.sampled_from(["CE", "PE"])


@given(
    exchange=valid_exchanges,
    segment=st.sampled_from(["EQ", "INDEX", "COM"]),
    symbol=valid_symbols,
)
def test_property_roundtrip_spot(exchange: str, segment: str, symbol: str) -> None:
    parts = IdParts(exchange=exchange, segment=segment, symbol=symbol)
    cid = format_id(parts)
    parsed = parse_id(cid)
    assert parsed.exchange == parts.exchange
    assert parsed.segment == parts.segment
    assert parsed.symbol == parts.symbol
    assert parsed.expiry is None
    assert parsed.strike is None
    assert parsed.option_type is None
    assert format_id(parsed) == cid


@given(
    exchange=valid_exchanges,
    symbol=valid_symbols,
    expiry=valid_dates,
)
def test_property_roundtrip_future(exchange: str, symbol: str, expiry: date) -> None:
    parts = IdParts(exchange=exchange, segment="FUT", symbol=symbol, expiry=expiry)
    cid = format_id(parts)
    parsed = parse_id(cid)
    assert parsed.exchange == parts.exchange
    assert parsed.segment == "FUT"
    assert parsed.symbol == parts.symbol
    assert parsed.expiry == expiry
    assert parsed.strike is None
    assert parsed.option_type is None
    assert format_id(parsed) == cid


@given(
    exchange=valid_exchanges,
    symbol=valid_symbols,
    expiry=valid_dates,
    strike=valid_strikes,
    option_type=valid_option_types,
)
def test_property_roundtrip_option(
    exchange: str,
    symbol: str,
    expiry: date,
    strike: Decimal,
    option_type: str,
) -> None:
    # Ensure positive strike
    k = abs(strike) if strike > 0 else Decimal("100.0")
    parts = IdParts(
        exchange=exchange,
        segment="OPT",
        symbol=symbol,
        expiry=expiry,
        strike=k,
        option_type=option_type,
    )
    cid = format_id(parts)
    parsed = parse_id(cid)
    assert parsed.exchange == parts.exchange
    assert parsed.segment == "OPT"
    assert parsed.symbol == parts.symbol
    assert parsed.expiry == expiry
    assert parsed.strike == k
    assert parsed.option_type == option_type
    assert format_id(parsed) == cid


# --- 2. Instrument object construction and field validation ---


def test_instrument_fields_and_canonical_id_auto_format() -> None:
    inst = Instrument(
        exchange="NSE",
        segment="FUT",
        symbol="RELIANCE",
        expiry=date(2026, 10, 27),
        lot_size=250,
        tick_size=Decimal("0.05"),
    )
    assert inst.id == InstrumentId("NSE:FUT:RELIANCE:2026-10-27")
    assert inst.underlying == "RELIANCE"
    assert inst.kind == "future"
    assert inst.lot_size == 250
    assert inst.tick_size == Decimal("0.05")

    # Option instrument
    opt = Instrument(
        exchange="NSE",
        segment="OPT",
        symbol="NIFTY",
        expiry=date(2026, 10, 29),
        strike=Decimal("24000"),
        option_type="CE",
        lot_size=50,
        tick_size=Decimal("0.05"),
    )
    assert opt.id == InstrumentId("NSE:OPT:NIFTY:2026-10-29:24000:CE")
    assert opt.kind == "option"


# --- 3. Upstox master mapping ---


def test_upstox_master_mapping_equity_and_derivatives() -> None:
    mgr = UpstoxInstrumentManager()

    # Equity record
    eq_row = {
        "instrument_key": "NSE_EQ|INE002A01018",
        "exchange": "NSE",
        "segment": "NSE_EQ",
        "instrument_type": "EQ",
        "trading_symbol": "RELIANCE",
        "lot_size": 1,
        "tick_size": 0.05,
    }
    eq_inst = mgr._map_row(eq_row)
    assert eq_inst.id == InstrumentId("NSE:EQ:RELIANCE")
    assert eq_inst.segment == "EQ"
    assert eq_inst.broker_keys["upstox"] == "NSE_EQ|INE002A01018"

    # Future record
    fut_row = {
        "instrument_key": "MCX_FO|495213",
        "exchange": "MCX",
        "segment": "MCX_FO",
        "instrument_type": "FUT",
        "trading_symbol": "GOLD FUT 04 DEC 26",
        "underlying_symbol": "GOLD",
        "expiry": 1796408999000,
        "lot_size": 1,
        "tick_size": 100.0,
    }
    fut_inst = mgr._map_row(fut_row)
    assert fut_inst.id == InstrumentId("MCX:FUT:GOLD:2026-12-04")
    assert fut_inst.segment == "FUT"
    assert fut_inst.underlying == "GOLD"
    assert fut_inst.expiry == date(2026, 12, 4)

    # Option record
    opt_row = {
        "instrument_key": "NSE_FO|50978",
        "exchange": "NSE",
        "segment": "NSE_FO",
        "instrument_type": "CE",
        "trading_symbol": "NIFTY 27000 CE 29 DEC 26",
        "underlying_symbol": "NIFTY",
        "strike_price": 27000.0,
        "expiry": 1798568999000,
        "lot_size": 65,
        "tick_size": 5.0,
    }
    opt_inst = mgr._map_row(opt_row)
    assert opt_inst.id == InstrumentId("NSE:OPT:NIFTY:2026-12-29:27000:CE")
    assert opt_inst.strike == Decimal("27000")
    assert opt_inst.option_type == "CE"


def test_upstox_unmappable_record_raises_hard_error() -> None:
    mgr = UpstoxInstrumentManager()

    # Missing instrument key
    with pytest.raises(DataError, match="missing or invalid instrument_key"):
        mgr._map_row({"foo": "bar"})

    # Missing expiry for future
    invalid_fut = {
        "instrument_key": "NSE_FO|12345",
        "exchange": "NSE",
        "segment": "NSE_FO",
        "instrument_type": "FUT",
        "trading_symbol": "BADFUT",
    }
    with pytest.raises(DataError, match="missing or invalid expiry date"):
        mgr._map_row(invalid_fut)

    # Missing strike for option
    invalid_opt = {
        "instrument_key": "NSE_FO|12346",
        "exchange": "NSE",
        "segment": "NSE_FO",
        "instrument_type": "CE",
        "trading_symbol": "BADOPT",
        "expiry": 1798568999000,
    }
    with pytest.raises(DataError, match="missing strike price"):
        mgr._map_row(invalid_opt)


# --- 4. yfinance maps equity only ---


def test_yfinance_maps_equity_only() -> None:
    yf = YFinanceBrokerPlugin(MagicMock(), YFinanceConfig())
    # Equity succeeds
    assert yf.map_symbol("NSE:EQ:RELIANCE") == "RELIANCE.NS"
    assert yf.map_symbol("BSE:EQ:RELIANCE") == "RELIANCE.BO"

    # Non-equity raises NotSupported
    with pytest.raises(NotSupported, match="equity only"):
        yf.map_symbol("NSE:FUT:RELIANCE:2026-10-27")

    with pytest.raises(NotSupported, match="equity only"):
        yf.map_symbol("NSE:OPT:NIFTY:2026-10-29:24000:CE")


# --- 5. Alias map resolution ---


def test_alias_map_resolves_legacy_ids() -> None:
    assert resolve_alias("MCX:FNO:GOLD FUT 04 DEC 26") == "MCX:FUT:GOLD:2026-12-04"
    assert resolve_alias("NSE:FNO:VEDL FUT 29 DEC 26") == "NSE:FUT:VEDL:2026-12-29"
    assert resolve_alias("NSE:FNO:NIFTY 27000 CE 29 DEC 26") == "NSE:OPT:NIFTY:2026-12-29:27000:CE"
    assert (
        resolve_alias("MCX:FNO:SILVER 226000 PE 26 MAY 27") == "MCX:OPT:SILVER:2027-05-26:226000:PE"
    )
    # Already canonical remains unchanged
    assert resolve_alias("NSE:EQ:INFY") == "NSE:EQ:INFY"
    assert resolve_alias("MCX:FUT:GOLD:2026-12-04") == "MCX:FUT:GOLD:2026-12-04"


def test_alias_map_ambiguity_fails_closed() -> None:
    # Register deliberate conflicting candidates
    register_alias("TEST:AMBIGUOUS:SYM", "NSE:EQ:SYM_A")
    # If explicit alias exists, it returns single
    assert resolve_alias("TEST:AMBIGUOUS:SYM") == "NSE:EQ:SYM_A"
