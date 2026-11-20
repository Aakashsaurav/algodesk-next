# Zerodha NSE Fee Schedule Plugin

## Summary
The `fees/zerodha_nse` plugin supplies verified dated fee and tax schedules for Zerodha trading on NSE and BSE. It defines itemised rate schedules for equity delivery, equity intraday, equity futures, and equity options across brokerage, Securities Transaction Tax (STT), exchange transaction charges, SEBI turnover fees, stamp duty, Depository Participant (DP) charges, and GST. The schedule is verified (`verified = true`, `verified_on = 2026-11-20`) against official Zerodha bulletins and zerodha.com/charges.

## Overview
This plugin loads and exposes `schedule.toml` for consumption by the core `FeeEngine`. All calculations preserve exact Decimal precision with component-level rounding. Effective 2026-11-02, stamp duty rounds to the nearest whole rupee half up per order.

### Covered Segments and Products
- **Equity Delivery (`eq:CNC`)**: Zero brokerage, 0.1% STT on buy and sell, 0.015% stamp duty on buy (rounded half up to whole rupee from 2026-11-02), dated NSE transaction charges (0.00325% from 2023-04-01, 0.00322% from 2024-04-01, 0.00297% from 2024-10-01, 0.00307% from 2026-10-30; BSE 0.00375% from 2026-10-30), ₹10/crore SEBI fee, ₹13.00 DP charge on sell (effective 2024-10-01, ₹15.34 including 18% GST), and 18% GST.
- **Equity Intraday (`eq:MIS`)**: Lower of ₹20 or 0.03% brokerage, 0.025% STT on sell, 0.003% stamp duty on buy (rounded half up to whole rupee from 2026-11-02), dated NSE transaction charges (0.00325% from 2023-04-01, 0.00322% from 2024-04-01, 0.00297% from 2024-10-01, 0.00307% from 2026-10-30; BSE 0.00375% from 2026-10-30), ₹10/crore SEBI fee, and 18% GST.
- **Equity Futures (`fno:NRML` & `fno:MIS`)**: Lower of ₹20 or 0.03% brokerage, STT (0.02% pre-2026-04-01, 0.05% effective 2026-04-01) on sell, NSE transaction charge 0.00183%, BSE transaction charge 0.0%, ₹10/crore SEBI fee, 18% GST, and 0.002% stamp duty on buy (rounded half up to whole rupee from 2026-11-02).
- **Equity Options (`fno:NRML` & `fno:MIS`)**: Flat ₹20 per order brokerage, STT 0.15% on sell of premium (effective 2026-04-01), NSE transaction charge 0.03553%, BSE transaction charge 0.0325%, ₹10/crore SEBI fee, 0.003% stamp duty on buy (rounded half up to whole rupee from 2026-11-02), and 18% GST. Exercise requires intrinsic valuation and raises `NotSupported`.

## Verification Status
- **Status**: `verified = true`, `verified_on = 2026-11-20`.
- **Method**: Official Zerodha circulars and bulletins verified on 20 November 2026. Prior delivery and intraday exchange transaction charges are dated: 0.00325% from 2023-04-01 (bulletin 347967), 0.00322% from 2024-04-01 (bulletin 373933), 0.00297% from 2024-10-01 (bulletin 391488), and 0.00307% from 2026-10-30. Delivery DP charge verified as ₹13.00 (₹3.50 CDSL + ₹9.50 Zerodha fee; ₹15.34 with 18% GST of ₹2.34) effective 2024-10-01 per bulletin 391488 and zerodha.com/charges.
- **Declared Components**: STT, stamp duty, SEBI turnover fees, and brokerage are declared, not independently verified for dates before verified_on (2026-11-20).
- **Stamp Duty Rounding**: Stamp duty rounds to the nearest whole rupee, half up, per order (e.g. 7.50 to 8, 1.50 to 2) with source `"owner check against Zerodha brokerage calculator, 2 Nov 2026"`.
- **Pre-verified Fallback**: Trades before earliest verified dates (2023-04-01 for equity exchange_txn, 2024-10-01 for DP) fail closed with FeeScheduleError unless `fees.fallback = "earliest"` is configured.

## Sources
- Zerodha Brokerage Calculator (zerodha.com/brokerage-calculator)
- Zerodha Charges List (zerodha.com/charges, support.zerodha.com)
- Zerodha Bulletin 347967 (https://zerodha.com/marketintel/bulletin/347967/revision-in-transaction-charges-and-stt-from-1st-april-2023)
- Zerodha Bulletin 373933 (https://zerodha.com/marketintel/bulletin/373933/revision-in-nse-transaction-charges-from-april-1-2024)
- Zerodha Bulletin 391488 (https://zerodha.com/marketintel/bulletin/391488/revision-in-transactions-charges-from-1st-october-2024)
- AlgoDesk PRD Appendix D.2 and Verification Fact VF-9
