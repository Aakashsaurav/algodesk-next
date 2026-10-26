"""Tests for Upstox instrument master download and mapping."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from plugins.brokers.upstox.instruments import UpstoxInstrumentManager

from algodesk.api import ConfigError, InstrumentId
from algodesk.api.kinds import Mode
from algodesk.ops.context import build_context

SAMPLE_INSTRUMENTS = [
    {
        "instrument_key": "NSE_EQ|INE002A01018",
        "exchange_token": "2885",
        "tradingsymbol": "RELIANCE",
        "name": "RELIANCE INDUSTRIES LTD",
        "last_price": 2500.0,
        "expiry": "",
        "strike": 0.0,
        "tick_size": 0.05,
        "lot_size": 1,
        "instrument_type": "EQUITY",
        "segment": "NSE_EQ",
        "exchange": "NSE",
    },
    {
        "instrument_key": "NSE_INDEX|Nifty 50",
        "exchange_token": "26000",
        "tradingsymbol": "Nifty 50",
        "name": "NIFTY 50",
        "last_price": 24000.0,
        "expiry": "",
        "strike": 0.0,
        "tick_size": 0.05,
        "lot_size": 25,
        "instrument_type": "INDEX",
        "segment": "NSE_INDEX",
        "exchange": "NSE",
    },
    {
        "instrument_key": "BSE_EQ|500325",
        "exchange_token": "500325",
        "tradingsymbol": "RELIANCE",
        "name": "RELIANCE INDUSTRIES LTD",
        "last_price": 2500.0,
        "expiry": "",
        "strike": 0.0,
        "tick_size": 0.05,
        "lot_size": 1,
        "instrument_type": "EQUITY",
        "segment": "BSE_EQ",
        "exchange": "BSE",
    },
]


def test_instruments_download_and_cache(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "complete.json" in str(request.url)
        return httpx.Response(200, json=SAMPLE_INSTRUMENTS)

    transport = httpx.MockTransport(handler)
    ctx = build_context(Mode.RESEARCH)
    mgr = UpstoxInstrumentManager(ctx, cache_dir=tmp_path, http_transport=transport)

    insts = mgr.load_or_fetch()
    assert len(insts) == 3

    # Verify cache file was written
    today = datetime.now(UTC).date()
    cache_file = tmp_path / f"upstox_master_{today.isoformat()}.json.gz"
    assert cache_file.is_file()

    # Second call should load from cache without HTTP request
    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("Should not be called")

    failing_mgr = UpstoxInstrumentManager(
        ctx, cache_dir=tmp_path, http_transport=httpx.MockTransport(failing_handler)
    )
    cached_insts = failing_mgr.load_or_fetch()
    assert len(cached_insts) == 3


def test_instruments_mapping_and_lookup(tmp_path: Path) -> None:
    today = datetime.now(UTC).date()
    cache_file = tmp_path / f"upstox_master_{today.isoformat()}.json.gz"
    cache_file.write_text(json.dumps(SAMPLE_INSTRUMENTS))

    ctx = build_context(Mode.RESEARCH)
    mgr = UpstoxInstrumentManager(ctx, cache_dir=tmp_path)
    mgr.load_or_fetch()

    # Lookup by symbol / ID
    rel = mgr.resolve("NSE:EQ:RELIANCE")
    assert rel is not None
    assert rel.id == InstrumentId("NSE:EQ:RELIANCE")
    assert rel.lot_size == 1
    assert rel.tick_size == Decimal("0.05")
    assert rel.broker_keys["upstox"] == "NSE_EQ|INE002A01018"

    # Index symbol lookup
    nifty = mgr.resolve("NIFTY 50")
    assert nifty is not None
    assert nifty.id == InstrumentId("NSE:EQ:NIFTY 50")
    assert nifty.lot_size == 25
    assert nifty.broker_keys["upstox"] == "NSE_INDEX|Nifty 50"


def test_instruments_unmappable_symbol_raises_hard_error(tmp_path: Path) -> None:
    today = datetime.now(UTC).date()
    cache_file = tmp_path / f"upstox_master_{today.isoformat()}.json.gz"
    cache_file.write_text(json.dumps(SAMPLE_INSTRUMENTS))

    ctx = build_context(Mode.RESEARCH)
    mgr = UpstoxInstrumentManager(ctx, cache_dir=tmp_path)
    mgr.load_or_fetch()

    with pytest.raises(ConfigError) as exc_info:
        mgr.resolve(InstrumentId("NSE:EQ:NONEXISTENT"))
    assert "Cannot map unknown symbol" in exc_info.value.what
