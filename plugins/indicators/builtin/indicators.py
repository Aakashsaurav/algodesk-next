"""Builtin technical indicator implementations (SMA, EMA, RSI)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from algodesk.api.protocols import IndicatorSpec

# ------------------------------------------------------------------------------
# Simple Moving Average (SMA)
# ------------------------------------------------------------------------------


@dataclass(slots=True)
class SMAState:
    """Incremental state for Simple Moving Average."""

    period: int
    buffer: list[float] = field(default_factory=list)
    running_sum: float = 0.0


class SMA:
    """Simple Moving Average calculated over a rolling window of n values."""

    def __init__(self, period: int = 14) -> None:
        if period <= 0:
            raise ValueError(f"SMA period must be a positive integer, got {period}")
        self.period = period
        self.spec = IndicatorSpec(
            name="sma",
            inputs=("close",),
            params_schema={"period": period},
            outputs=("value",),
            warmup=period - 1,
        )

    def new_state(self) -> SMAState:
        """Create a fresh incremental calculation state."""
        return SMAState(period=self.period)

    def update(self, state: object, value: float) -> float | None:
        """Process a single observation incrementally without look-ahead."""
        if not isinstance(state, SMAState):
            raise TypeError(f"Expected SMAState, got {type(state).__name__}")

        st = state
        st.buffer.append(value)
        st.running_sum += value

        if len(st.buffer) < st.period:
            return None

        if len(st.buffer) > st.period:
            removed = st.buffer.pop(0)
            st.running_sum -= removed

        return st.running_sum / st.period

    def compute(self, values: Sequence[float]) -> Sequence[float | None]:
        """Compute indicator values over a sequence sharing the incremental logic."""
        st = self.new_state()
        return [self.update(st, v) for v in values]


# ------------------------------------------------------------------------------
# Exponential Moving Average (EMA)
# ------------------------------------------------------------------------------


@dataclass(slots=True)
class EMAState:
    """Incremental state for Exponential Moving Average."""

    period: int
    alpha: float
    buffer: list[float] = field(default_factory=list)
    prev_ema: float | None = None


class EMA:
    """Exponential Moving Average (alpha=2/(n+1)), seeded with the first n-values SMA."""

    def __init__(self, period: int = 14) -> None:
        if period <= 0:
            raise ValueError(f"EMA period must be a positive integer, got {period}")
        self.period = period
        self.alpha = 2.0 / (period + 1.0)
        self.spec = IndicatorSpec(
            name="ema",
            inputs=("close",),
            params_schema={"period": period},
            outputs=("value",),
            warmup=period - 1,
        )

    def new_state(self) -> EMAState:
        """Create a fresh incremental calculation state."""
        return EMAState(period=self.period, alpha=self.alpha)

    def update(self, state: object, value: float) -> float | None:
        """Process a single observation incrementally without look-ahead."""
        if not isinstance(state, EMAState):
            raise TypeError(f"Expected EMAState, got {type(state).__name__}")

        st = state
        if st.prev_ema is None:
            st.buffer.append(value)
            if len(st.buffer) < st.period:
                return None
            initial_sma = sum(st.buffer) / st.period
            st.prev_ema = initial_sma
            return initial_sma

        new_ema = (st.alpha * value) + ((1.0 - st.alpha) * st.prev_ema)
        st.prev_ema = new_ema
        return new_ema

    def compute(self, values: Sequence[float]) -> Sequence[float | None]:
        """Compute indicator values over a sequence sharing the incremental logic."""
        st = self.new_state()
        return [self.update(st, v) for v in values]


# ------------------------------------------------------------------------------
# Relative Strength Index (RSI - Wilder)
# ------------------------------------------------------------------------------


@dataclass(slots=True)
class RSIState:
    """Incremental state for Wilder's Relative Strength Index."""

    period: int
    prev_price: float | None = None
    gains: list[float] = field(default_factory=list)
    losses: list[float] = field(default_factory=list)
    avg_gain: float | None = None
    avg_loss: float | None = None


class RSI:
    """Wilder's Relative Strength Index with smoothed average gains and losses."""

    def __init__(self, period: int = 14) -> None:
        if period <= 0:
            raise ValueError(f"RSI period must be a positive integer, got {period}")
        self.period = period
        self.spec = IndicatorSpec(
            name="rsi",
            inputs=("close",),
            params_schema={"period": period},
            outputs=("value",),
            warmup=period,
        )

    def new_state(self) -> RSIState:
        """Create a fresh incremental calculation state."""
        return RSIState(period=self.period)

    def update(self, state: object, value: float) -> float | None:
        """Process a single price observation incrementally without look-ahead."""
        if not isinstance(state, RSIState):
            raise TypeError(f"Expected RSIState, got {type(state).__name__}")

        st = state
        if st.prev_price is None:
            st.prev_price = value
            return None

        delta = value - st.prev_price
        st.prev_price = value
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)

        if st.avg_gain is None or st.avg_loss is None:
            st.gains.append(gain)
            st.losses.append(loss)
            if len(st.gains) < st.period:
                return None
            st.avg_gain = sum(st.gains) / st.period
            st.avg_loss = sum(st.losses) / st.period
        else:
            st.avg_gain = ((st.avg_gain * (st.period - 1)) + gain) / st.period
            st.avg_loss = ((st.avg_loss * (st.period - 1)) + loss) / st.period

        if st.avg_loss == 0.0:
            return 100.0 if st.avg_gain > 0.0 else 50.0

        if st.avg_gain == 0.0:
            return 0.0

        rs = st.avg_gain / st.avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def compute(self, values: Sequence[float]) -> Sequence[float | None]:
        """Compute indicator values over a sequence sharing the incremental logic."""
        st = self.new_state()
        return [self.update(st, v) for v in values]
