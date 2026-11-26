# Status

Owner: Aakash · Version: 1.1 · Date: 26 November 2026

## Summary

Milestones M0–M4 are complete. AlgoDesk Next provides a layered core and plugin workbench with TOML config, SQLite parameter registry, hash-chained journal, scrubbed logging, deterministic execution engine, historical data layer, indicators/screens, and reproducible backtester with reporting. Next: M5 design (risk and control).

## Milestones

| M | Name | State |
|---|---|---|
| M0 | Foundations | Done |
| M1 | Walking skeleton | Done |
| M2 | Data layer | Done |
| M3 | Indicators and screens | Done |
| M4 | Backtester and reports | Done |
| M5 to M9 | Risk, Zerodha, remote, extended paper, live | Planned |

## M4 Evidence

- **Paper/Replay**: `demo_paper_fast_paper_20261126_071953_9822c5` vs `demo_paper_fast_replay_20261126_101716_3875d9` equal `True`, no duplicate BUY, kill-switch CLEARED (2026-11-22). 25 Nov `demo_paper_fast_paper_20261125_070106_77666d` vs `demo_paper_fast_replay_20261125_105627_187c0d` equal `True`.
- **Backtest reproducibility**: `sma_1d_bt` and `rotation_1d_bt` reproduce byte-for-byte (`equal: True`, hashes equal `True`).
- **Bar-fidelity equivalence**: 16 Nov 46/46 equal (`True`); 25 Nov 1/123 differs (ICICIBANK SELL 09:46 UTC flat zero-volume bar vs 09:45 timer exit); 19 Nov 4 signals differ.
- **Speed (no profiler)**: 100 x 250 (1d) 18–20 s (1,239–1,365 bars/s, ~710 us/bar); 20 x 20 (1m) ~110 s (~707 us/bar); 100 x 250 (1m) ~1.8 hours (calculated).
- **Calibration**: 19 Nov (16 adverse pairs): median 1.86 bps, 95th 6.21 bps; default `backtest.slippage_bps = 5`.
- **Charges**: Rates verified 2026-11-20 and 2026-11-21 applied to earlier dates (declared).

## Known Limits

1. Survivorship bias (current index list).
2. Prices split-adjusted, not raw.
3. Dividends not credited.
4. Historical fee rates declared constant before verification.
5. MCX charges declared, not independently verified.
6. 1m backtests slow (~11 min for 10 symbols x 1 year, calculated).
7. Bar-fidelity equivalence not exact on flat zero-volume bars at `exit_time`.
8. Persistent kill-switch trips (daily loss stops entries until cleared).
9. Paper fills optimistic (no queue priority or market impact).

## Optional Work & Next

- Optional: speed to 3x, per-rebalance membership (M4-9), equivalence on flat bars at `exit_time`.
- **Next**: M5 design (risk and control).
