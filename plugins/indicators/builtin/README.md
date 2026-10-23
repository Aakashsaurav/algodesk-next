# Builtin Indicators Plugin

## Summary
The `indicators/builtin` plugin provides core mathematical indicators for AlgoDesk Next: Simple Moving Average (SMA), Exponential Moving Average (EMA), and Relative Strength Index (RSI - Wilder). All indicators implement identical logic across batch `compute()` and incremental `update()` pipelines with zero look-ahead bias and exact None warmup values.

## Indicator Specifications

### Simple Moving Average (SMA)
- **Period**: Default $n = 14$ ($n \ge 1$).
- **Warmup**: First $n - 1$ values are `None`.
- **Formula**: Simple arithmetic mean of the last $n$ inputs.

### Exponential Moving Average (EMA)
- **Period**: Default $n = 14$ ($n \ge 1$).
- **Smoothing factor**: $\alpha = \frac{2}{n + 1}$.
- **Seed**: Exactly equal to the SMA of the first $n$ inputs at index $n - 1$.
- **Recursive formula**: $\text{EMA}_t = \alpha \cdot x_t + (1 - \alpha) \cdot \text{EMA}_{t-1}$.

### Relative Strength Index (RSI - Wilder)
- **Period**: Default $n = 14$ ($n \ge 1$).
- **Initial averages**: Arithmetic means of first $n$ positive gains and negative losses across $n + 1$ prices.
- **Smoothing**: Wilder smoothing factor $\frac{n - 1}{n}$.
- **Boundary conditions**: 100.0 when average loss is 0 and average gain > 0; 50.0 when both are 0.
