# Simulated Venue (sim)

## Summary

The simulated execution venue (`venues/sim`) provides conservative simulated order execution for research, backtesting, and paper trading without live market connectivity (`order_capable = false`). Orders incur simulated latency with seeded jitter (0 ms in bar-mode backtest) and are priced against the first quote received strictly at or after arrival (`quote.recv_ts >= order.arrival`). Market orders walk order book depth; limit orders fill marketably or rest until traded through; `STOP` and `STOP_MARKET` orders trigger when quotes cross `trigger_price`. Paper fills are optimistic regarding queue priority and market impact.

## Bar Model & Stops

1. **Bar-Mode Quote Path**: `BarQuoteSynth` synthesizes 4 quotes per bar (`O -> L -> H -> C` for up bars, `O -> H -> L -> C` for down bars). Orders decided on bar $t$ arrive at the Open quote of bar $t+1$ and fill at `round_to_tick(open(t+1) +/- slippage)` without look-ahead.
2. **Stop Orders**: Resting `STOP` and `STOP_MARKET` orders trigger when a synthetic or live quote crosses `trigger_price`. A stop crossed by a bar's opening quote fills at the open price.

## Paper-Fill Limitations (FR-PAP-6)

1. **Queue Priority**: Limit fills rely on trade-through (`sim.through_ticks`) rather than exchange queue priority.
2. **Market Impact**: Simulated orders do not consume exchange liquidity or move subsequent quotes.
3. **Deterministic Clock**: Uses incoming quote/order timestamps for deterministic replay with a given seed.
4. **Verification**: `verified_on = 2026-10-27`, source: AlgoDesk Next Architecture Specification §5.2.
