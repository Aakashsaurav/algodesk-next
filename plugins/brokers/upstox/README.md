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

## Verified Endpoints & Rate Limits (Official Documentation 31 Oct 2026)
- **Rate Limits**:
  - Standard APIs: 50 requests/sec, 500 requests/min, 2,000 requests/30min enforced per API and per user (`upstox.com/developer/api-documentation/rate-limiting`).
  - Core rate limiter starts Upstox at 80% of published limits (40/s, 400/m, 1,600/30m).
- **History V3 Endpoint**:
  - `GET https://api.upstox.com/v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}` (`upstox.com/developer/api-documentation/v3/get-historical-candle-data`).
  - Instrument key must be URL-encoded (e.g. `NSE_EQ%7CINE002A01018`), dates in `YYYY-MM-DD`.
  - Intraday minutes (`1m` to `15m`): Available from Jan 2022, max 1 month per request span. Over the limit raises an upstream error.
  - Daily (`1d`): Available from Jan 2000, max 1 decade (10 years) per request span.
  - Candle format: `[timestamp IST ISO, open, high, low, close, volume, open interest]`.
  - Adjustments: Upstox candles are stored with `adj=adjusted` and manifest field `candles_adjusted = "unknown"` with a provenance warning until verified post-spike.

## Changes in M2-4 (2026-10-31)
- **Corporate Actions Reader**: Implemented `corporate_actions(symbol)` using official endpoint `GET https://api.upstox.com/v2/corporate-actions/{isin}` keyed by ISIN from instrument master. Rate-limited per API and user under limiter key `corporate_actions`.
- **Adjustment Basis**: Upstox candles are split/bonus adjusted (verified via spike 1 Nov 2026: RELIANCE bonus ex-date 2024-10-28, ratio 1.0049); dividend treatment remains unknown. Declared `candles_adjustment_basis = "unknown"` in `plugin.toml` until verified post-comparison spike. Views refuse to treat unknown as total.
- **Unverified Events**: If the Upstox response lacks split or bonus ratio fields, events are marked `unverified` and require operator override or two-source reconciliation.

## Changes in M2-2 (2026-10-31)
- **history.py**: Added URL-encoding of instrument keys, mapping 4xx HTTP responses to typed errors (`RateLimitError` for 429, `AuthError` for 401/403, `DataError` for 400/404).
- **plugin.toml**: Declared rate limits, retention, and max span chunk boundaries.
- **plugin.py**: Added `actions()` corporate actions probing endpoint.

## Changes after M1-6 (2026-10-30)
- **auth.py & plugin.py**: Added `open_browser` toggle to `begin_auto()` and guarded `webbrowser.open()` to prevent popping host browsers during automated test runs.
- **instruments.py**: Added `_cleanup_older_downloads()` to prune stale instrument master downloads in `.local/cache/instruments/`, enforcing disk hygiene and test hermeticity.
