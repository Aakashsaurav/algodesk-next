"""Tests for Upstox instrument master IST daily caching and refresh-master."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx
from plugins.brokers.upstox.instruments import IST, UpstoxInstrumentManager

from algodesk.api.kinds import Mode
from algodesk.ops.context import build_context

SAMPLE_DATA = [
    {
        "instrument_key": "NSE_EQ|INE002A01018",
        "exchange_token": "2885",
        "tradingsymbol": "RELIANCE",
        "name": "RELIANCE",
        "last_price": 2500.0,
        "expiry": "",
        "strike": 0.0,
        "tick_size": 0.05,
        "lot_size": 1,
        "instrument_type": "EQUITY",
        "segment": "NSE_EQ",
        "exchange": "NSE",
    }
]


def test_master_cache_ist_same_day_hit(tmp_path: Path, capsys: object) -> None:
    today_ist = datetime.now(IST).date()
    cache_file = tmp_path / f"upstox_master_{today_ist.isoformat()}.json.gz"
    cache_file.write_text(json.dumps(SAMPLE_DATA))

    ctx = build_context(Mode.RESEARCH)
    mgr = UpstoxInstrumentManager(ctx, cache_dir=tmp_path)

    insts = mgr.load_or_fetch()
    assert len(insts) == 1
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "master cached" in captured.out


def test_master_cache_refresh_forces_new_download(tmp_path: Path, monkeypatch: object) -> None:
    today_ist = datetime.now(IST).date()
    cache_file = tmp_path / f"upstox_master_{today_ist.isoformat()}.json.gz"
    cache_file.write_text(json.dumps(SAMPLE_DATA))

    download_called = False

    def mock_download(request: httpx.Request) -> httpx.Response:
        nonlocal download_called
        download_called = True
        return httpx.Response(200, json=SAMPLE_DATA)

    ctx = build_context(Mode.RESEARCH)
    transport = httpx.MockTransport(mock_download)
    mgr = UpstoxInstrumentManager(ctx, cache_dir=tmp_path, http_transport=transport)

    # 1. With force_refresh=True
    insts1 = mgr.load_or_fetch(force_refresh=True)
    assert len(insts1) == 1
    assert download_called is True

    # 2. With env var UPSTOX_REFRESH_MASTER=1
    download_called = False
    monkeypatch.setenv("UPSTOX_REFRESH_MASTER", "1")  # type: ignore[attr-defined]
    insts2 = mgr.load_or_fetch()
    assert len(insts2) == 1
    assert download_called is True
