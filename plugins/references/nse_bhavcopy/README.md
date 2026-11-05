# NSE Official Bhavcopy Reference Plugin

## Summary
Independent reference plugin downloading and parsing daily Bhavcopy archives from the National Stock Exchange of India (NSE). Conforms to `daily_bars(symbols_or_isins, start, end)` and `DataSource` protocols, ingesting unadjusted raw 1d candlestick bars with `adjustment_basis="none"`. Transparently supports both legacy format (pre-July 2024) and unified UDiFF format (post-July 2024) with symbol and ISIN universe filtering. Provides official exchange trading-day signals: missing weekday files (>1 day old, 2 attempts) record closure kind `exchange_file`, while weekend files record special trading sessions.

---

## Capabilities & Architecture
- **Protocols**: Implements `daily_bars(symbols_or_isins, start, end)` and `DataSource` (`history`, `instruments`).
- **Archive URL Schemes**:
  - Legacy (`d < 2024-07-08`): `https://nsearchives.nseindia.com/content/historical/EQUITIES/YYYY/MON/cmDDMONYYYYbhav.csv.zip`
  - Unified UDiFF (`d >= 2024-07-08`): `https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip`
- **Trading-Day Signals**:
  - Weekday without Bhavcopy file after 2 attempts (>1 day old): Recorded in `market_closures` as `kind="exchange_file"`.
  - Weekend with Bhavcopy file: Recorded as `kind="special"`.
- **Adjustment Basis**:
  - All series ingested with `adj="raw"` and `adjustment_basis="none"`.
  - Converted dynamically at read time via `DataService.get(..., adjust="none" | "splits" | "total")` using confirmed corporate actions.
- **Rate Limits & Safeguards**: Governed by core `RateLimiter` under key `(nse_bhavcopy, bhavcopy)` (1 req/s, 30 req/min, 600 req/30min).

