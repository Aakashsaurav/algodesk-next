# Yahoo Finance Market Data Plugin

## Summary
The yfinance broker plugin implements the `DataSource` protocol for research and historical backtesting over Yahoo Finance. It maps Indian equity scrips on NSE (`.NS`) and BSE (`.BO`), supporting standard timeframe intervals from 1-minute to daily with raw (`auto_adjust=False`) or dividend/split-adjusted (`auto_adjust=True`) OHLCV bars. Historical queries enforce retention limits through the central rate limiter and parameter registry.

## Verified Specifications (31 Oct 2026)
- **Timeframe & Reach**:
  - `1m`: Intraday 1-minute history reaches approximately 7 days.
  - `1d`: Daily history spans multiple decades.
- **Adjustment Modes**:
  - `download="adjusted"` uses `auto_adjust=True` (default).
  - `download="raw"` uses `auto_adjust=False`.
  - Output is strictly timezone-aware UTC and monotonic ascending.
- **Rate Limits & Throttling**:
  - Limits declared in `plugin.toml`: 2 requests/sec, 60 requests/min, 1,000 requests/30min.
  - Self-throttling removed; requests are throttled centrally by core `RateLimiter` with exponential backoff on HTTP 429.

## Constraints & Usage
- `live_suitable = false`, `realtime = false`, `official_api = false`.
- `stream()` and `instruments()` raise `NotSupported`.
- Extended methods: `actions()`, `fundamentals()`, `info()`, `options_chain()`.
