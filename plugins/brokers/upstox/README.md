# Upstox Broker Plugin

## Summary
Upstox broker plugin for AlgoDesk Next providing market data streaming via WebSocket V3 (Protobuf), historical and intraday candle fetching, instrument master mapping with local caching, and dual-mode authentication (long-lived analytics tokens and automated/manual OAuth2 authorization).

---

## Capabilities & Architecture
- **Protocol**: Implements `DataSource` (`instruments`, `history`, `stream`) and `AuthFlow` (`begin`, `begin_auto`, `complete`, `use_token`, `validate`, `expires_at`).
- **Data Feed**: Connects to Upstox V3 Market Data Feed WebSocket, subscribing in `full` mode with 5-level market depth. Decodes binary frames via vendored Protobuf (`MarketDataFeedV3_pb2.py`).
- **Auth Priority**: Defaults to `["analytics", "oauth"]`. Analytics token operates market data feeds; OAuth tokens are stored in the secure SecretsProvider.
- **Instruments**: Daily download and caching of Upstox instrument master, normalized to canonical `InstrumentId` with lot and tick sizes.
- **Verified Endpoints**:
  - Auth dialog: `https://api.upstox.com/v2/login/authorization/dialog` (verified 2026-10-23)
  - Token exchange: `https://api.upstox.com/v2/login/authorization/token` (verified 2026-10-23)
  - Feed authorize: `https://api.upstox.com/v3/feed/market-data-feed/authorize` (verified 2026-10-23)
  - Instruments: `https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz` (verified 2026-10-23)
  - History: `https://api.upstox.com/v3/historical-candle/...` (verified 2026-10-23)
