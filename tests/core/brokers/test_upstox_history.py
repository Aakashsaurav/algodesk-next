"""Tests for Upstox historical candles client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from plugins.brokers.upstox.history import UpstoxHistoryClient
from plugins.brokers.upstox.instruments import UpstoxInstrumentManager

from algodesk.api import AlgoDeskError, Instrument, InstrumentId, SeriesKey
from algodesk.api.kinds import Mode
from algodesk.ops.context import build_context

SAMPLE_CANDLES_DAILY = {
    "status": "success",
    "data": {
        "candles": [
            ["2026-10-22T00:00:00+05:30", 2500.0, 2550.0, 2490.0, 2540.0, 1000000, 0],
            ["2026-10-21T00:00:00+05:30", 2480.0, 2510.0, 2470.0, 2495.0, 950000, 0],
        ]
    },
}

SAMPLE_CANDLES_INTRADAY = {
    "status": "success",
    "data": {
        "candles": [
            ["2026-10-22T15:25:00+05:30", 2535.0, 2542.0, 2530.0, 2540.0, 50000, 0],
            ["2026-10-22T15:20:00+05:30", 2530.0, 2538.0, 2528.0, 2536.0, 45000, 0],
        ]
    },
}


def _setup_inst_mgr(tmp_path: Path, ctx: Any) -> tuple[UpstoxInstrumentManager, InstrumentId]:
    inst_mgr = UpstoxInstrumentManager(ctx, cache_dir=tmp_path)
    inst_id = InstrumentId("NSE:EQ:RELIANCE")
    inst = Instrument(
        id=inst_id,
        exchange="NSE",
        segment="EQ",
        symbol="RELIANCE",
        kind="equity",
        lot_size=1,
        tick_size=Decimal("0.05"),
        broker_keys={"upstox": "NSE_EQ|INE002A01018"},
    )
    inst_mgr._instruments[str(inst_id)] = inst
    inst_mgr._key_to_inst["NSE_EQ|INE002A01018"] = inst
    inst_mgr._symbol_to_inst["RELIANCE"] = inst
    return inst_mgr, inst_id


def test_history_daily_candles(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "historical-candle" in str(request.url)
        assert "/days/1/" in str(request.url)
        return httpx.Response(200, json=SAMPLE_CANDLES_DAILY)

    transport = httpx.MockTransport(handler)
    ctx = build_context(Mode.RESEARCH)
    inst_mgr, inst_id = _setup_inst_mgr(tmp_path, ctx)

    client = UpstoxHistoryClient(inst_mgr, ctx, http_transport=transport)
    key = SeriesKey(
        instrument=inst_id, kind="bars", timeframe="1d", adjustment="raw", source="upstox"
    )

    now = datetime.now(UTC)
    bars = list(client.history(key, now - timedelta(days=5), now))

    assert len(bars) == 2
    # Verify chronological ordering (oldest first)
    assert bars[0].start_ts < bars[1].start_ts
    assert bars[0].open == 2480.0
    assert bars[0].close == 2495.0
    assert bars[1].open == 2500.0
    assert bars[1].close == 2540.0


def test_history_intraday_candles(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "historical-candle" in str(request.url)
        assert "/minutes/5/" in str(request.url)
        return httpx.Response(200, json=SAMPLE_CANDLES_INTRADAY)

    transport = httpx.MockTransport(handler)
    ctx = build_context(Mode.RESEARCH)
    inst_mgr, inst_id = _setup_inst_mgr(tmp_path, ctx)

    client = UpstoxHistoryClient(inst_mgr, ctx, http_transport=transport)
    key = SeriesKey(
        instrument=inst_id, kind="bars", timeframe="5m", adjustment="raw", source="upstox"
    )

    now = datetime.now(UTC)
    bars = list(client.history(key, now - timedelta(hours=2), now))

    assert len(bars) == 2
    assert bars[0].start_ts < bars[1].start_ts
    assert bars[0].open == 2530.0
    assert bars[1].close == 2540.0


def test_history_http_error_raises_algodesk_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"status": "error", "message": "Invalid date range"})

    transport = httpx.MockTransport(handler)
    ctx = build_context(Mode.RESEARCH)
    inst_mgr, inst_id = _setup_inst_mgr(tmp_path, ctx)

    client = UpstoxHistoryClient(inst_mgr, ctx, http_transport=transport)
    key = SeriesKey(
        instrument=inst_id, kind="bars", timeframe="1d", adjustment="raw", source="upstox"
    )

    now = datetime.now(UTC)
    with pytest.raises(AlgoDeskError) as exc_info:
        list(client.history(key, now - timedelta(days=5), now))
    assert "Candle history request failed" in exc_info.value.what
