"""Tests for Upstox Protobuf WebSocket feed decoding and lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from plugins.brokers.upstox.feed import UpstoxWebSocketFeed
from plugins.brokers.upstox.instruments import UpstoxInstrumentManager
from plugins.brokers.upstox.proto import MarketDataFeedV3_pb2

from algodesk.api import AlgoDeskError, Instrument, InstrumentId, Quote, Tick
from algodesk.api.kinds import Mode
from algodesk.ops.context import build_context


def _setup_feed_instrument(
    inst_mgr: UpstoxInstrumentManager, inst_id: InstrumentId, key: str, sym: str
) -> None:
    inst = Instrument(
        id=inst_id,
        exchange="NSE",
        segment="EQ",
        symbol=sym,
        kind="equity",
        lot_size=1,
        tick_size=Decimal("0.05"),
        broker_keys={"upstox": key},
    )
    inst_mgr._instruments[str(inst_id)] = inst
    inst_mgr._key_to_inst[key] = inst
    inst_mgr._symbol_to_inst[sym] = inst


def test_feed_decode_protobuf_snapshot(tmp_path: Path) -> None:
    ctx = build_context(Mode.RESEARCH)
    inst_mgr = UpstoxInstrumentManager(ctx, cache_dir=tmp_path)
    inst_id = InstrumentId("NSE:EQ:RELIANCE")
    _setup_feed_instrument(inst_mgr, inst_id, "NSE_EQ|INE002A01018", "RELIANCE")

    feed = UpstoxWebSocketFeed(inst_mgr, ctx)

    # Build synthetic Protobuf FeedResponse with full snapshot
    resp = MarketDataFeedV3_pb2.FeedResponse()
    resp.type = MarketDataFeedV3_pb2.Type.initial_feed
    feed_data = resp.feeds["NSE_EQ|INE002A01018"]
    full_feed = feed_data.fullFeed
    market_ff = full_feed.marketFF

    market_ff.ltpc.ltp = 2500.50
    market_ff.ltpc.ltt = 1729670000000
    market_ff.ltpc.ltq = 10
    market_ff.ltpc.cp = 2490.0
    market_ff.vtt = 500000
    market_ff.oi = 1000

    # 5-level depth
    for i in range(5):
        lvl = market_ff.marketLevel.bidAskQuote.add()
        lvl.bidQ = (i + 1) * 10
        lvl.bidP = 2500.0 - (i * 0.5)
        lvl.askQ = (i + 1) * 15
        lvl.askP = 2501.0 + (i * 0.5)

    raw_bytes = resp.SerializeToString()
    events = feed.decode_frame(raw_bytes)

    assert len(events) == 2
    quotes = [e for e in events if isinstance(e, Quote)]
    ticks = [e for e in events if isinstance(e, Tick)]
    assert len(quotes) == 1
    assert len(ticks) == 1
    quote = quotes[0]
    assert quote.instrument == inst_id
    assert quote.ltp == 2500.50
    assert quote.volume == 500000
    assert len(quote.bids) == 5
    assert len(quote.asks) == 5
    assert quote.bids[0].price == 2500.0
    assert quote.asks[0].price == 2501.0


def test_feed_decode_live_tick(tmp_path: Path) -> None:
    ctx = build_context(Mode.RESEARCH)
    inst_mgr = UpstoxInstrumentManager(ctx, cache_dir=tmp_path)
    inst_id = InstrumentId("NSE:EQ:INFY")
    _setup_feed_instrument(inst_mgr, inst_id, "NSE_EQ|INE009A01021", "INFY")

    feed = UpstoxWebSocketFeed(inst_mgr, ctx)

    resp = MarketDataFeedV3_pb2.FeedResponse()
    resp.type = MarketDataFeedV3_pb2.Type.initial_feed
    feed_data = resp.feeds["NSE_EQ|INE009A01021"]
    live_feed = feed_data.fullFeed.marketFF
    live_feed.ltpc.ltp = 1900.25
    live_feed.ltpc.ltq = 5
    live_feed.ltpc.ltt = 1729670001000

    raw_bytes = resp.SerializeToString()
    events = feed.decode_frame(raw_bytes)

    assert len(events) == 1
    assert isinstance(events[0], Tick)
    assert events[0].instrument == inst_id
    assert events[0].ltp == 1900.25


def test_feed_decode_malformed_frame_skipped(tmp_path: Path) -> None:
    ctx = build_context(Mode.RESEARCH)
    inst_mgr = UpstoxInstrumentManager(ctx, cache_dir=tmp_path)
    feed = UpstoxWebSocketFeed(inst_mgr, ctx)

    # Corrupt / garbage bytes should be logged/skipped, not raise
    events = feed.decode_frame(b"\x00\xff\xfe\xfa\x12\x34garbage")
    assert events == []


def test_feed_limits_check(tmp_path: Path) -> None:
    ctx = build_context(Mode.RESEARCH)
    inst_mgr = UpstoxInstrumentManager(ctx, cache_dir=tmp_path)
    feed = UpstoxWebSocketFeed(inst_mgr, ctx)

    # Exceeding 2000 keys limit
    too_many_keys = [f"KEY_{i}" for i in range(2001)]
    with pytest.raises(AlgoDeskError) as exc_keys:
        feed._check_limits(too_many_keys)
    assert "exceeds limit" in exc_keys.value.what

    # Exceeding 2 connections limit
    UpstoxWebSocketFeed._active_connections = 2
    try:
        with pytest.raises(AlgoDeskError) as exc_conn:
            feed._check_limits(["KEY_1"])
        assert "Active feed connections" in exc_conn.value.what
    finally:
        UpstoxWebSocketFeed._active_connections = 0


def test_feed_staleness_tracking(tmp_path: Path) -> None:
    ctx = build_context(Mode.RESEARCH)
    inst_mgr = UpstoxInstrumentManager(ctx, cache_dir=tmp_path)
    feed = UpstoxWebSocketFeed(inst_mgr, ctx)
    inst_id = InstrumentId("NSE:EQ:RELIANCE")

    # Not seen yet -> stale
    assert feed.is_stale(inst_id) is True

    # Mark seen now
    feed._last_seen[inst_id] = datetime.now(UTC)
    assert feed.is_stale(inst_id) is False
