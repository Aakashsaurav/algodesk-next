# Screen Rotation Strategy Plugin

## Summary
The `strategies/screen_rotation` plugin implements a deterministic, long-only screen-driven portfolio rotation strategy for `research` and `backtest` modes (`order_capable = false`). At each rebalance interval, it evaluates a registered market screen point-in-time via `ctx.screen()` as of the last completed session, ranks passing symbols by score and symbol, and executes a two-step rebalance to rotate holdings into the top `top_n` instruments.

## Parameters
- `screen`: Name of screen to run at each rebalance (default `"trend_rank_test"`).
- `group`: Optional instrument group name (default `None`).
- `top_n`: Number of top-ranked instruments to hold (default `5`).
- `weight_pct`: Target portfolio weight percentage per position (default `15.0`).
- `rebalance_every`: Trading sessions between portfolio rebalances (default `21`).
- `timeframe`: Bar timeframe for rebalance trigger (default `"1d"`).
- `product`: Order product type (default `"CNC"`).

## Mechanics & Reporting
- **Two-step rebalance**: Departing symbols are sold at session close; replacement BUY orders are emitted on the first completed bar after sell fills settle, sized from current equity and free cash.
- **Artifact**: Writes `rebalances.csv` (rebalance date, screen hash, selected symbols, intended and fill-executed buys/sells, rejects).

## Verification
- `verified_on = 2026-11-21`
- Sources: AlgoDesk Next Rotation Backtest Section FR-BKT-5, ADR-089, ADR-090
