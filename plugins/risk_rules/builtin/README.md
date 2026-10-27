# Builtin Risk Rules Plugin

## Summary
The Builtin Risk Rules plugin implements nine standard pre-trade safety controls for Indian equity and derivative trading on AlgoDesk Next. Rules enforce margin cash buffers, position size caps, gross exposure limits, order value safeguards, price deviation checks, order frequency throttles, daily trade counts, and daily loss boundaries. All quantitative thresholds are loaded dynamically from parameter registries under `risk.<profile>.*`. Weekly loss limits and peak-to-trough drawdown halt controls are scheduled for milestone M5 and are documented as not enforced in M1.

## Enforced Rules (M1)

1. `cash_available`: Ensures projected cash after order placement remains at or above `risk.<profile>.margin_buffer_pct` of allocated capital.
2. `max_order_value_pct`: Limits gross value of any single order against fat-finger errors.
3. `max_position_pct`: Restricts maximum single position size relative to portfolio capital.
4. `max_open_positions`: Caps concurrent open positions.
5. `max_gross_exposure_pct`: Restricts total portfolio gross market exposure.
6. `price_deviation_pct`: Validates limit order prices against the current best bid/ask or last traded price.
7. `max_orders_per_minute`: Throttles submission velocity below broker and venue rate limits.
8. `max_daily_trades`: Enforces daily turnover and trade count caps to prevent overtrading.
9. `daily_loss_limit_pct`: Prevents new position entries once daily loss reaches or exceeds the profile ceiling.

## Deferred Rules (M5)

- `weekly_loss_limit_pct`: Weekly cumulative loss limit is deferred to M5 multi-session lifecycle.
- `drawdown_halt_pct`: Peak-to-trough multi-day equity drawdown circuit breaker is deferred to M5.
