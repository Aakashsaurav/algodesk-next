# Builtin Indicators Plugin

## Summary
The `indicators/builtin` plugin provides ten core mathematical technical indicators for AlgoDesk Next: Simple Moving Average (SMA), Exponential Moving Average (EMA), Relative Strength Index (RSI - Wilder), Average True Range (ATR - Wilder), Volume Weighted Average Price (VWAP), Bollinger Bands, Moving Average Convergence Divergence (MACD), Rolling High/Low (RollingHL), Volume Average (VolumeAvg), and Rate of Change (ROC). All indicators implement identical mathematical definitions across batch `compute()` and incremental `update()` pipelines with zero look-ahead bias and exact `None` warm-up outputs.

## Indicator Specifications and Conventions

### 1. Simple Moving Average (SMA)
- **Parameters**: `n = 14` ($n \ge 1$, alias `period`).
- **Inputs**: `("close",)`.
- **Outputs**: `("value",)`.
- **Warm-up**: First $n - 1$ values are `None`.
- **Formula**: Simple arithmetic mean of the last $n$ inputs.

### 2. Exponential Moving Average (EMA)
- **Parameters**: `n = 14` ($n \ge 1$, alias `period`).
- **Inputs**: `("close",)`.
- **Outputs**: `("value",)`.
- **Warm-up**: First $n - 1$ values are `None`.
- **Smoothing factor**: $\alpha = \frac{2}{n + 1}$.
- **Seed**: Exactly equal to the SMA of the first $n$ inputs at index $n - 1$.
- **Recursive formula**: $\text{EMA}_t = \alpha \cdot x_t + (1 - \alpha) \cdot \text{EMA}_{t-1}$.

### 3. Relative Strength Index (RSI - Wilder)
- **Parameters**: `n = 14` ($n \ge 1$, alias `period`).
- **Inputs**: `("close",)`.
- **Outputs**: `("value",)`.
- **Warm-up**: First $n$ values are `None`.
- **Initial averages**: Arithmetic means of first $n$ gains and losses across $n + 1$ prices.
- **Smoothing**: Wilder smoothing factor $\frac{n - 1}{n}$.
- **Boundary conditions**: 100.0 when average loss is 0.0 and average gain > 0.0; 50.0 when both are 0.0.

### 4. Average True Range (ATR - Wilder)
- **Parameters**: `n = 14` ($n \ge 1$, alias `period`).
- **Inputs**: `("high", "low", "close")`.
- **Outputs**: `("atr",)`.
- **Warm-up**: First $n - 1$ values are `None`.
- **True Range**:
  - Bar 0: $\text{TR}_0 = \text{high}_0 - \text{low}_0$.
  - Bar $t > 0$: $\text{TR}_t = \max(\text{high}_t - \text{low}_t, |\text{high}_t - \text{close}_{t-1}|, |\text{low}_t - \text{close}_{t-1}|)$.
- **Seed**: Arithmetic mean of the first $n$ true ranges at index $n - 1$.
- **Smoothing**: $\text{ATR}_t = \frac{\text{ATR}_{t-1} \cdot (n - 1) + \text{TR}_t}{n}$.

### 5. Volume Weighted Average Price (VWAP)
- **Parameters**: None (session-reset).
- **Inputs**: `("high", "low", "close", "volume", "new_session")`.
- **Outputs**: `("vwap",)`.
- **Warm-up**: 0 (returns typical price on first bar if volume > 0).
- **Allowed timeframes**: Intraday only (`timeframes = ("intraday",)`). Daily or weekly requests raise `ValueError`.
- **Formula**: Cumulative $(\text{high} + \text{low} + \text{close}) / 3 \times \text{volume}$ divided by cumulative volume, resetting on session boundary. Zero cumulative volume returns `None`.

### 6. Bollinger Bands
- **Parameters**: `n = 20` ($n \ge 1$), `k = 2.0` ($k > 0$).
- **Inputs**: `("close",)`.
- **Outputs**: `("middle", "upper", "lower")`.
- **Warm-up**: First $n - 1$ values return `None` for all bands.
- **Formula**:
  - $\text{Middle} = \text{SMA}(n)$.
  - Population standard deviation $\sigma = \sqrt{\frac{1}{n} \sum_{i=1}^n (x_i - \text{Middle})^2}$.
  - $\text{Upper} = \text{Middle} + k \cdot \sigma$.
  - $\text{Lower} = \text{Middle} - k \cdot \sigma$.

### 7. Moving Average Convergence Divergence (MACD)
- **Parameters**: `fast = 12`, `slow = 26`, `signal = 9` ($1 \le \text{fast} < \text{slow}$, $\text{signal} \ge 1$).
- **Inputs**: `("close",)`.
- **Outputs**: `("macd", "signal", "histogram")`.
- **Warm-up**: First $\text{slow} + \text{signal} - 2$ values return `None` for signal and histogram.
- **Formula**:
  - $\text{MACD Line} = \text{EMA}(\text{fast}) - \text{EMA}(\text{slow})$.
  - $\text{Signal Line} = \text{EMA}(\text{signal})$ of the MACD line, seeded with the SMA of the first `signal` values of the MACD line.
  - $\text{Histogram} = \text{MACD Line} - \text{Signal Line}$.

### 8. Rolling High / Low (RollingHL)
- **Parameters**: `n = 20` ($n \ge 1$, alias `period`).
- **Inputs**: `("high", "low")`.
- **Outputs**: `("high", "low")`.
- **Warm-up**: First $n - 1$ values return `None` for both outputs.
- **Formula**: Highest high and lowest low of the last $n$ completed bars including the current bar.

### 9. Volume Average (VolumeAvg)
- **Parameters**: `n = 20` ($n \ge 1$, alias `period`).
- **Inputs**: `("volume",)`.
- **Outputs**: `("volume_avg",)`.
- **Warm-up**: First $n - 1$ values are `None`.
- **Formula**: Simple arithmetic mean of volume over the rolling window of $n$ bars.

### 10. Rate of Change (ROC)
- **Parameters**: `n = 12` ($n \ge 1$, alias `period`).
- **Inputs**: `("close",)`.
- **Outputs**: `("roc",)`.
- **Warm-up**: First $n$ values are `None`.
- **Formula**: $\frac{\text{close}_t}{\text{close}_{t-n}} - 1.0$ (expressed as a fraction, not percentage). Zero divisor returns `None`.

## Verification Status
- Verified on: `2026-11-13`.
