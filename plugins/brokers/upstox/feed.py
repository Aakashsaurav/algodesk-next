"""Upstox Market Data Feed WebSocket client with Protobuf decoding."""

from __future__ import annotations

import asyncio
import json
import random
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import websockets

from algodesk.api import (
    AlgoDeskError,
    FeedStatus,
    InstrumentId,
    Level,
    MarketEvent,
    PluginContext,
    Quote,
    Tick,
)

from .instruments import UpstoxInstrumentManager
from .proto import MarketDataFeedV3_pb2


class UpstoxWebSocketFeed:
    """Streams live market data from Upstox V3 WebSocket feed."""

    _active_connections: int = 0

    def __init__(
        self,
        instruments: UpstoxInstrumentManager,
        ctx: PluginContext | None = None,
        *,
        http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._instruments = instruments
        self._ctx = ctx
        self._transport = http_transport
        self._last_seen: dict[InstrumentId, datetime] = {}
        self._is_running = False

    def _param(self, name: str, default: Any) -> Any:
        if self._ctx is not None and self._ctx.params is not None:
            try:
                val = self._ctx.params.get(name)
                return val if val is not None else default
            except Exception:
                return default
        return default

    def authorize_feed(self, token: str) -> str:
        """Fetch authorized WebSocket redirect URL."""
        url = "https://api.upstox.com/v3/feed/market-data-feed/authorize"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        timeout = float(self._param("upstox.request_timeout_s", 15.0))
        with httpx.Client(transport=self._transport, timeout=timeout) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code != 200:
                raise AlgoDeskError(
                    what=f"Feed authorization failed with HTTP {resp.status_code}",
                    where="UpstoxWebSocketFeed.authorize_feed",
                    fix="Verify Upstox access token validity",
                )
            payload = resp.json()

        redirect_uri: str = payload.get("data", {}).get("authorized_redirect_uri", "")
        if not redirect_uri:
            raise AlgoDeskError(
                what="Missing authorized_redirect_uri in response",
                where="UpstoxWebSocketFeed.authorize_feed",
                fix="Inspect Upstox feed authorize response envelope",
            )
        return redirect_uri

    def _check_limits(self, keys: list[str]) -> None:
        max_keys = int(self._param("upstox.max_subscribed_keys", 2000))
        if len(keys) > max_keys:
            raise AlgoDeskError(
                what=f"Subscription request for {len(keys)} keys exceeds limit of {max_keys}",
                where="UpstoxWebSocketFeed._check_limits",
                fix=f"Reduce subscription keys to at most {max_keys}",
            )
        max_conns = int(self._param("upstox.max_connections", 2))
        if UpstoxWebSocketFeed._active_connections >= max_conns:
            conns = UpstoxWebSocketFeed._active_connections
            raise AlgoDeskError(
                what=f"Active feed connections ({conns}) reach limit of {max_conns}",
                where="UpstoxWebSocketFeed._check_limits",
                fix="Reuse existing WebSocket feed connection",
            )

    def is_stale(self, instrument_id: InstrumentId) -> bool:
        """Check if market data for an instrument is stale."""
        last = self._last_seen.get(instrument_id)
        if last is None:
            return True
        thresh = float(self._param("upstox.staleness_threshold_s", 10.0))
        return (datetime.now(UTC) - last).total_seconds() > thresh

    async def stream(
        self,
        instruments: Sequence[InstrumentId],
        token: str,
        *,
        ws_url_override: str | None = None,
    ) -> AsyncIterator[MarketEvent]:
        """Connect to WebSocket, decode Protobuf frames, and yield MarketEvents."""
        upstox_keys: list[str] = []
        for i_id in instruments:
            inst = self._instruments.resolve(i_id)
            k = inst.broker_keys.get("upstox")
            if k:
                upstox_keys.append(k)

        self._check_limits(upstox_keys)
        UpstoxWebSocketFeed._active_connections += 1
        self._is_running = True

        try:
            ws_url = ws_url_override or self.authorize_feed(token)
            async for ev in self._stream_with_reconnect(ws_url, upstox_keys):
                yield ev
        finally:
            self._is_running = False
            UpstoxWebSocketFeed._active_connections = max(
                0, UpstoxWebSocketFeed._active_connections - 1
            )

    async def _stream_with_reconnect(
        self,
        ws_url: str,
        upstox_keys: list[str],
    ) -> AsyncIterator[MarketEvent]:
        base_delay = float(self._param("upstox.reconnect_base_delay_s", 1.0))
        max_delay = float(self._param("upstox.reconnect_max_delay_s", 30.0))
        jitter = float(self._param("upstox.reconnect_jitter_s", 0.5))
        attempt = 0
        disconnect_ts: datetime | None = None

        while self._is_running:
            try:
                async with websockets.connect(ws_url) as ws:
                    attempt = 0
                    now_utc = datetime.now(UTC)
                    yield FeedStatus(state="connected", ts=now_utc, details="WebSocket connected")
                    if disconnect_ts is not None:
                        yield FeedStatus(
                            state="gap",
                            ts=now_utc,
                            gap_start=disconnect_ts,
                            gap_end=now_utc,
                            details="Feed gap during reconnection",
                        )
                        disconnect_ts = None

                    sub_payload = {
                        "guid": str(uuid.uuid4()),
                        "method": "sub",
                        "data": {"instrumentKeys": upstox_keys, "mode": "full"},
                    }
                    await ws.send(json.dumps(sub_payload).encode("utf-8"))

                    async for msg in ws:
                        if not self._is_running:
                            break
                        if isinstance(msg, bytes):
                            for ev in self._decode_frame(msg):
                                yield ev

            except (websockets.ConnectionClosed, OSError, asyncio.CancelledError) as exc:
                if not self._is_running or isinstance(exc, asyncio.CancelledError):
                    break
                disconnect_ts = datetime.now(UTC)
                yield FeedStatus(state="disconnected", ts=disconnect_ts, details=str(exc))
                delay = min(base_delay * (2**attempt), max_delay) + random.uniform(0.0, jitter)
                attempt += 1
                await asyncio.sleep(delay)

    def decode_frame(self, raw: bytes) -> list[MarketEvent]:
        """Decode raw Protobuf bytes frame into MarketEvents."""
        return self._decode_frame(raw)

    def _decode_frame(self, raw: bytes) -> list[MarketEvent]:
        try:
            resp = MarketDataFeedV3_pb2.FeedResponse.FromString(raw)
        except Exception as exc:
            if self._ctx is not None and self._ctx.journal is not None:
                try:
                    self._ctx.journal.append(
                        "feed.malformed_frame", {"error": str(exc), "len": len(raw)}
                    )
                except Exception:
                    pass
            return []

        events: list[MarketEvent] = []
        recv_ts = datetime.now(UTC)
        for key, feed in resp.feeds.items():
            try:
                inst = self._instruments.resolve(key)
            except Exception:
                continue

            self._last_seen[inst.id] = recv_ts
            field_name = feed.WhichOneof("FeedUnion")
            if field_name == "fullFeed":
                ff = feed.fullFeed
                ff_choice = ff.WhichOneof("FullFeedUnion")
                if ff_choice == "marketFF":
                    mff = ff.marketFF
                    self._parse_market_ff(inst.id, mff, recv_ts, events)
                elif ff_choice == "indexFF":
                    self._parse_index_ff(inst.id, ff.indexFF, recv_ts, events)
            elif field_name == "ltpc":
                self._parse_ltpc(inst.id, feed.ltpc, recv_ts, events)
        return events

    def _parse_market_ff(
        self, i_id: InstrumentId, mff: Any, recv_ts: datetime, events: list[MarketEvent]
    ) -> None:
        ltpc = mff.ltpc
        exch_ts = datetime.fromtimestamp(ltpc.ltt / 1000.0, tz=UTC) if ltpc.ltt else recv_ts
        ltp = float(ltpc.ltp)
        ltq = int(ltpc.ltq) if ltpc.ltq > 0 else 1
        volume = int(mff.vtt)
        oi = int(mff.oi) if mff.oi > 0 else None

        events.append(
            Tick(
                instrument=i_id,
                exch_ts=exch_ts,
                recv_ts=recv_ts,
                ltp=ltp,
                ltq=ltq,
                volume=volume,
                oi=oi,
            )
        )
        bids = [
            Level(price=float(q.bidP), qty=int(q.bidQ))
            for q in mff.marketLevel.bidAskQuote
            if q.bidP > 0 and q.bidQ > 0
        ]
        asks = [
            Level(price=float(q.askP), qty=int(q.askQ))
            for q in mff.marketLevel.bidAskQuote
            if q.askP > 0 and q.askQ > 0
        ]
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)
        if bids and asks and bids[0].price > asks[0].price:
            return
        if bids or asks:
            events.append(
                Quote(
                    instrument=i_id,
                    exch_ts=exch_ts,
                    recv_ts=recv_ts,
                    bids=tuple(bids[:5]),
                    asks=tuple(asks[:5]),
                    ltp=ltp,
                    volume=volume,
                )
            )

    def _parse_index_ff(
        self, i_id: InstrumentId, iff: Any, recv_ts: datetime, events: list[MarketEvent]
    ) -> None:
        ltpc = iff.ltpc
        exch_ts = datetime.fromtimestamp(ltpc.ltt / 1000.0, tz=UTC) if ltpc.ltt else recv_ts
        events.append(
            Tick(
                instrument=i_id,
                exch_ts=exch_ts,
                recv_ts=recv_ts,
                ltp=float(ltpc.ltp),
                ltq=1,
                volume=0,
            )
        )

    def _parse_ltpc(
        self, i_id: InstrumentId, ltpc: Any, recv_ts: datetime, events: list[MarketEvent]
    ) -> None:
        exch_ts = datetime.fromtimestamp(ltpc.ltt / 1000.0, tz=UTC) if ltpc.ltt else recv_ts
        events.append(
            Tick(
                instrument=i_id,
                exch_ts=exch_ts,
                recv_ts=recv_ts,
                ltp=float(ltpc.ltp),
                ltq=int(ltpc.ltq or 1),
                volume=0,
            )
        )
