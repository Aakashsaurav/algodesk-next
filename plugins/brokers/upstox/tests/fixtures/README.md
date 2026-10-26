# Upstox Protocol Test Fixtures

## Summary
Recorded and synthetic protocol fixtures for Upstox broker plugin testing, covering Protobuf binary frames (initial feed snapshot, live ticks, market info) and JSON REST response envelopes (OAuth token exchange, WebSocket authorization, daily historical candles, and instrument master records).

---

## Fixture Index & Provenance
- `feed_snapshot.bin`: Synthetic Protobuf binary frame representing `initial_feed` with `MarketFullFeed` 5-level depth.
- `feed_live_tick.bin`: Synthetic Protobuf binary frame representing `live_feed` tick update.
- `feed_market_info.bin`: Synthetic Protobuf binary frame representing `market_info` exchange segment status.
- `auth_token_response.json`: Sample JSON payload from `POST /v2/login/authorization/token`.
- `feed_authorize_response.json`: Sample JSON payload from `GET /v3/feed/market-data-feed/authorize`.
- `candles_1d_response.json`: Sample JSON payload from `GET /v3/historical-candle/...`.
- `instruments_sample.json`: Sample JSON records matching Upstox instrument master schema.
