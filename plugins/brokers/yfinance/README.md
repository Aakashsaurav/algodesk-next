# Yahoo Finance Market Data Plugin

## Summary
The yfinance broker plugin implements the `DataSource` protocol for research and historical backtesting over Yahoo Finance. It maps Indian equity scrips on NSE (`.NS`) and BSE (`.BO`), supporting standard timeframe intervals from 1-minute to daily with raw (`auto_adjust=False`) or dividend/split-adjusted (`auto_adjust=True`) OHLCV bars. Historical queries enforce retention limits through the central rate limiter and parameter registry.

## Verified Specifications (31 Oct 2026)
- **Timeframe & Reach**:
  - `1m`: Intraday 1-minute history reaches approximately 7 days.
  - `1d`: Daily history spans multiple decades.
- **Adjustment Modes & Basis Facts (Spike 2 Nov 2026, RELIANCE 699 days)**:
  - `download="adjusted"` uses `auto_adjust=True` (default). Reflects dividend reinvestment adjustments alongside splits (`total` basis).
  - `download="raw"` uses `auto_adjust=False`. Close series has no break at split or bonus dates and is already split- and bonus-adjusted (`splits` basis). It is not truly raw prices.
  - Declared `[candles_adjustment_basis]` in `plugin.toml`: `raw = "splits"`, `adjusted = "total"`.
  - Output is strictly timezone-aware UTC and monotonic ascending.
- **Corporate Actions**:
  - Read via `Ticker.actions`, extracting split ratios and dividends per share.
  - Declared `capability = ["corporate_actions"]` in `plugin.toml`.
- **Rate Limits & Throttling**:
  - Limits declared in `plugin.toml`: 2 requests/sec, 60 requests/min, 1,000 requests/30min.
  - Self-throttling removed; requests are throttled centrally by core `RateLimiter` with exponential backoff on HTTP 429.

## Constraints & Usage
- `live_suitable = false`, `realtime = false`, `official_api = false`.
- `stream()` and `instruments()` raise `NotSupported`.
- Extended methods: `actions()`, `fundamentals()`, `info()`, `options_chain()`.
