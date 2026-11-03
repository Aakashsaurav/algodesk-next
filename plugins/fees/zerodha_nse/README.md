# Zerodha NSE Fee Schedule Plugin

## Summary
The `fees/zerodha_nse` plugin supplies verified dated fee and tax schedules for Zerodha trading on NSE and BSE. It defines itemised rate schedules for equity delivery, equity intraday, equity futures, and equity options across brokerage, Securities Transaction Tax (STT), exchange transaction charges, SEBI turnover fees, stamp duty, Depository Participant (DP) charges, and GST. The schedule is verified (`verified = true`, `verified_on = 2026-11-02`) against Zerodha's official brokerage calculator by hand checks within 1 rupee.

## Overview
This plugin loads and exposes `schedule.toml` for consumption by the core `FeeEngine`. All calculations preserve exact Decimal precision with component-level rounding. Effective 2026-11-02, stamp duty rounds to the nearest whole rupee half up per order.

### Covered Segments and Products
- **Equity Delivery (`eq:CNC`)**: Zero brokerage, 0.1% STT on buy and sell, 0.015% stamp duty on buy (rounded half up to whole rupee from 2026-11-02), NSE/BSE transaction charges (0.00307% NSE, 0.00375% BSE), ₹10/crore SEBI fee, ₹13.50 DP charge on sell, and 18% GST.
- **Equity Intraday (`eq:MIS`)**: Lower of ₹20 or 0.03% brokerage, 0.025% STT on sell, 0.003% stamp duty on buy (rounded half up to whole rupee from 2026-11-02), NSE/BSE transaction charges (0.00307% NSE, 0.00375% BSE), ₹10/crore SEBI fee, and 18% GST.
- **Equity Futures (`fno:NRML` & `fno:MIS`)**: Lower of ₹20 or 0.03% brokerage, STT (0.02% pre-2026-04-01, 0.05% effective 2026-04-01) on sell, NSE transaction charge 0.00183%, BSE transaction charge 0.0%, ₹10/crore SEBI fee, 18% GST, and 0.002% stamp duty on buy (rounded half up to whole rupee from 2026-11-02).
- **Equity Options (`fno:NRML` & `fno:MIS`)**: Flat ₹20 per order brokerage, STT 0.15% on sell of premium (effective 2026-04-01), NSE transaction charge 0.03553%, BSE transaction charge 0.0325%, ₹10/crore SEBI fee, 0.003% stamp duty on buy (rounded half up to whole rupee from 2026-11-02), and 18% GST. Exercise requires intrinsic valuation and raises `NotSupported`.

## Verification Status
- **Status**: `verified = true`, `verified_on = 2026-11-02`.
- **Method**: Eight owner sample trade vectors across equity delivery and equity options on both NSE and BSE were checked by hand against Zerodha's official brokerage calculator on 2 November 2026. All computed trade totals matched the calculator totals within 1 rupee (all within 0.02 rupee).
- **Stamp Duty Rounding**: Stamp duty rounds to the nearest whole rupee, half up, per order (e.g. 7.50 to 8, 1.50 to 2) with source `"owner check against Zerodha brokerage calculator, 2 Nov 2026"`.

## Sources
- Zerodha Brokerage Calculator (zerodha.com/brokerage-calculator)
- Zerodha Charge List (support.zerodha.com)
- AlgoDesk PRD Appendix D.2 and Verification Fact VF-9
