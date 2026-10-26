"""Comprehensive unit tests for Upstox feed limits, staleness, and timeframe parsing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from plugins.brokers.upstox.feed import UpstoxWebSocketFeed
from plugins.brokers.upstox.history import UpstoxHistoryClient
from plugins.brokers.upstox.instruments import UpstoxInstrumentManager

from algodesk.api import (
    ConfigError,
    Instrument,
    InstrumentId,
    Quote,
)
from algodesk.api.kinds import Mode
from algodesk.ops.context import build_context


def _make_dummy_inst_mgr(tmp_path: Path, ctx: Any) -> UpstoxInstrumentManager:
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
    return inst_mgr


@pytest.mark.parametrize(
    "tf_str,expected_unit,expected_interval",
    [
        ("1d", "days", "1"),
        ("1D", "days", "1"),
        ("1m", "minutes", "1"),
        ("5m", "minutes", "5"),
        ("15m", "minutes", "15"),
        ("30m", "minutes", "30"),
        ("60m", "minutes", "60"),
        ("1h", "hours", "1"),
        ("2h", "hours", "2"),
        ("4h", "hours", "4"),
    ],
)
def test_upstox_history_parse_timeframe_valid(
    tmp_path: Path, tf_str: str, expected_unit: str, expected_interval: str
) -> None:
    ctx = build_context(Mode.RESEARCH)
    inst_mgr = _make_dummy_inst_mgr(tmp_path, ctx)
    client = UpstoxHistoryClient(inst_mgr, ctx)
    unit, interval = client._parse_timeframe(tf_str)
    assert unit == expected_unit
    assert interval == expected_interval


@pytest.mark.parametrize(
    "invalid_tf",
    [
        "2m",
        "10m",
        "45m",
        "1w",
        "2w",
        "tick",
        "daily",
        "intraday",
        "",
        "bad",
    ],
)
def test_upstox_history_parse_timeframe_invalid(tmp_path: Path, invalid_tf: str) -> None:
    ctx = build_context(Mode.RESEARCH)
    inst_mgr = _make_dummy_inst_mgr(tmp_path, ctx)
    client = UpstoxHistoryClient(inst_mgr, ctx)
    with pytest.raises(ConfigError) as exc_info:
        client._parse_timeframe(invalid_tf)
    assert "Unsupported Upstox timeframe" in exc_info.value.what


@pytest.mark.parametrize(
    "elapsed_seconds,expected_stale",
    [
        (0.5, False),
        (2.0, False),
        (5.0, False),
        (8.0, False),
        (9.9, False),
        (10.1, True),
        (15.0, True),
        (30.0, True),
        (60.0, True),
        (300.0, True),
    ],
)
def test_upstox_feed_staleness_parameterized(
    tmp_path: Path, elapsed_seconds: float, expected_stale: bool
) -> None:
    ctx = build_context(Mode.RESEARCH)
    inst_mgr = _make_dummy_inst_mgr(tmp_path, ctx)
    feed = UpstoxWebSocketFeed(inst_mgr, ctx)
    inst_id = InstrumentId("NSE:EQ:RELIANCE")

    feed._last_seen[inst_id] = datetime.now(UTC) - timedelta(seconds=elapsed_seconds)
    assert feed.is_stale(inst_id) is expected_stale


def test_upstox_feed_parse_depth_sorting(tmp_path: Path) -> None:
    ctx = build_context(Mode.RESEARCH)
    inst_mgr = _make_dummy_inst_mgr(tmp_path, ctx)
    feed = UpstoxWebSocketFeed(inst_mgr, ctx)
    inst_id = InstrumentId("NSE:EQ:RELIANCE")

    events: list[Any] = []
    recv_ts = datetime.now(UTC)

    class DummyQuoteItem:
        def __init__(self, bp: float, bq: int, ap: float, aq: int) -> None:
            self.bidP = bp
            self.bidQ = bq
            self.askP = ap
            self.askQ = aq

    class DummyMarketLevel:
        def __init__(self, quotes: list[DummyQuoteItem]) -> None:
            self.bidAskQuote = quotes

    class DummyLTPC:
        ltp = 2500.0
        ltq = 10
        ltt = 1729670000000
        cp = 2490.0

    class DummyMFF:
        ltpc = DummyLTPC()
        vtt = 100000
        oi = 500
        # Quotes intentionally unsorted
        marketLevel = DummyMarketLevel(
            [
                DummyQuoteItem(2498.0, 50, 2503.0, 30),
                DummyQuoteItem(2500.0, 100, 2501.0, 40),
                DummyQuoteItem(2499.0, 80, 2502.0, 60),
            ]
        )

    feed._parse_market_ff(inst_id, DummyMFF(), recv_ts, events)
    assert len(events) == 2
    quote = [e for e in events if isinstance(e, Quote)][0]

    # Bids should be sorted descending by price
    assert [b.price for b in quote.bids] == [2500.0, 2499.0, 2498.0]
    # Asks should be sorted ascending by price
    assert [a.price for a in quote.asks] == [2501.0, 2502.0, 2503.0]
