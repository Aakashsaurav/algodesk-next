# SMA Cross Strategy Plugin

## Summary
The `strategies/sma_cross` plugin implements a deterministic long-only Simple Moving Average crossover strategy for paper trading and backtesting in AlgoDesk Next. It evaluates completed bars, entering a long position when the fast SMA crosses strictly above the slow SMA, and exiting when the fast SMA crosses below the slow SMA, upon reaching the intraday exit time, or when a configured stop-loss percentage is breached.

> [!NOTE]
> This strategy is a demonstration designed solely to test the workbench pipeline. It is not investment advice or a recommendation to trade.

## Parameters
- `instruments`: Explicit list of instrument identifiers (e.g. `["NSE:EQ:RELIANCE"]`).
- `group`: Optional instrument group name (e.g. `"demo5"`).
- `timeframe`: Bar aggregation timeframe (default `"5m"`).
- `fast`: Fast SMA lookback period (default `20`).
- `slow`: Slow SMA lookback period (default `50`).
- `qty`: Order quantity per entry (default `10`).
- `product`: Order product type, either `"cnc"` or `"mis"` (default `"mis"`).
- `exit_time`: Intraday square-off time string (default `"15:15"`).
- `stop_pct`: Optional stop-loss percentage from entry price (e.g. `0.02` for 2%).
