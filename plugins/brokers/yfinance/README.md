# Yahoo Finance Market Data Plugin

## Summary
The yfinance broker plugin implements the `DataSource` protocol for research and historical backtesting over Yahoo Finance. It maps Indian equity scrips on NSE (`.NS`) and BSE (`.BO`), supporting standard timeframe intervals from 1-minute to daily with raw or dividend/split-adjusted OHLCV bars. Retention limits are enforced via parameter registry thresholds, raising clear configuration errors when historical queries exceed upstream limits. Rate limiting uses a token bucket with exponential backoff on HTTP 429 errors.

## Constraints & Usage
- `live_suitable = false`, `realtime = false`, `official_api = false`.
- `stream()` and `instruments()` raise `NotSupported`.
- Extended methods: `actions()`, `fundamentals()`, `info()`, `options_chain()`.
