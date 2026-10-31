# Zerodha NSE Fee Schedule Plugin

## Summary
The `fees/zerodha_nse` plugin supplies verified dated fee and tax schedules for Zerodha trading on the National Stock Exchange of India (NSE). It defines itemised rate schedules for equity delivery, equity intraday, and equity futures across brokerage, Securities Transaction Tax (STT), exchange transaction charges, SEBI turnover fees, stamp duty, Depository Participant (DP) charges, and Goods and Services Tax (GST).

## Overview
This plugin loads and exposes `schedule.toml` for consumption by the core `FeeEngine`. All calculations preserve exact Decimal precision with component-level half-up rounding.

### Covered Segments and Products
- **Equity Delivery (`eq:CNC`)**: Zero brokerage, 0.1% STT on buy and sell, 0.015% stamp duty on buy, 0.00297% NSE transaction charge, ₹10/crore SEBI fee, ₹13.50 DP charge on sell, and 18% GST.
- **Equity Intraday (`eq:MIS`)**: Lower of ₹20 or 0.03% brokerage, 0.025% STT on sell, 0.003% stamp duty on buy, 0.00297% NSE transaction charge, ₹10/crore SEBI fee, and 18% GST.
- **Equity Futures (`fno:NRML` & `fno:MIS`)**: Lower of ₹20 or 0.03% brokerage, STT (0.02% pre-2026-04-01, 0.05% effective 2026-04-01) on sell, NSE transaction charge 0.00183%, BSE transaction charge 0.0%, ₹10/crore SEBI fee, 18% GST, and 0.002% stamp duty on buy.
- **Equity Options (`fno:NRML` & `fno:MIS`)**: Flat ₹20 per order brokerage, STT 0.15% on sell of premium (effective 2026-04-01), NSE transaction charge 0.03553%, BSE transaction charge 0.0325%, ₹10/crore SEBI fee, 0.003% stamp duty on buy, and 18% GST. Exercise requires intrinsic valuation and raises `NotSupported`.

## Changes after M1-6 (2026-10-30)
- **schedule.toml**: Added dated entries for 2026-04-01 STT notice (futures STT 0.05%, options STT 0.15% of premium on sell), updated exchange transaction charges (NSE/BSE for equity, futures, options), added options flat brokerage ₹20, and set `verified = false` pending owner calculator check.

## Sources
- Zerodha Brokerage Calculator (zerodha.com/brokerage-calculator)
- Zerodha Charge List (support.zerodha.com)
- AlgoDesk PRD Appendix D.2 and Verification Fact VF-9
