# Simulated Venue (sim)

## Summary

The simulated execution venue (`venues/sim`) provides realistic, conservative simulated order execution for research, backtesting, and paper trading without live market connectivity. Orders incur simulated latency with seeded jitter and are priced against the first quote received strictly at or after arrival. Market orders walk order book depth level-by-level; limit orders fill marketably or rest until traded through. Paper fills are inherently optimistic regarding queue priority and market impact, so actual live execution may differ.

## Paper-Fill Limitations (FR-PAP-6)

Simulated execution models realistic order book mechanics but has fundamental differences from live trading:
1. **Queue Priority**: Limit order fills rely on price trade-through rather than queue priority modeling. Fills assume execution once the market trades through by `sim.through_ticks`.
2. **Market Impact**: Simulated orders do not consume exchange liquidity or influence subsequent market quotes.
3. **No Network Clock**: The venue uses timestamps strictly from incoming market quotes and order submissions, ensuring deterministic replay with a given seed.
4. **Order Capable**: The plugin manifest explicitly declares `order_capable = false` to guarantee no live orders can ever be submitted through this venue.
