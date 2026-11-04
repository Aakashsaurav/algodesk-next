# NSE Corporate Actions Reference Plugin

## Summary
Independent reference plugin fetching historical and current corporate actions directly from the National Stock Exchange of India (NSE). Conforms to the `ReferenceSource` protocol and provides deterministic parsing of exchange purpose texts into canonical `ActionRecord` types (splits, bonuses, dividends, and rights). Raw API responses are persisted with date stamps under `data/actions/raw/nse/` for provenance. Verified against official NSE corporate actions data on 3 November 2026.

---

## Capabilities & Architecture
- **Protocol**: Implements `ReferenceSource` (`corporate_actions(symbol_or_isin, start=None, end=None)`).
- **Endpoint**: `https://www.nseindia.com/api/corporates-corporateActions?index=equities`
- **Supported Query Filters**: `symbol=<SYMBOL>`, `from_date=<DD-MM-YYYY>`, `to_date=<DD-MM-YYYY>`.
- **Parsing Rules**:
  - **Split**: Extracted from `Face Value Split (Sub-Division) - From Rs X/- Per Share To Rs Y/- Per Share` yielding `ratio_old=X`, `ratio_new=Y`, `ratio=X/Y`.
  - **Bonus**: Extracted from `Bonus X:Y` yielding `ratio_new=X`, `ratio_old=Y`, `ratio=X/Y`.
  - **Dividend**: Rupee amounts extracted from purpose text and summed into `Decimal(amount)`.
  - **Rights**: Ratio extracted; if explicit price stated, populated as `issue_price`; otherwise flagged `manual_required` ("never guess").
  - **Other / Unrecognised**: Flagged `manual_required` preserving verbatim raw text.
- **Raw Cache**: Responses cached to `data/actions/raw/nse/{symbol}_{date}.json`.
- **Rate Limits**: Governed by core `RateLimiter` under key `(nse, corporate_actions)` (1 req/s, 30 req/min, 600 req/30min).
