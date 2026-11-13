"""Builtin technical indicator implementations.

Exposes standard single- and multi-input, single- and multi-output indicators:
SMA, EMA, RSI, ATR, VWAP, Bollinger, MACD, RollingHL, VolumeAvg, ROC.
All indicators implement identical batch compute() and incremental update() logic.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any
from zoneinfo import ZoneInfo

from algodesk.api.protocols import IndicatorSpec

_IST = ZoneInfo("Asia/Kolkata")


def _extract_field(value: Any, field_name: str, default: float = 0.0) -> float:
    """Extract a numeric field from a Bar, mapping, or scalar."""
    if hasattr(value, field_name):
        return float(getattr(value, field_name))
    if isinstance(value, Mapping) and field_name in value:
        return float(value[field_name])
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _extract_rows(values: Sequence[Any] | Mapping[str, Sequence[Any]]) -> list[Any]:
    """Convert input series or column-oriented mapping into row sequence."""
    if isinstance(values, Mapping):
        keys = list(values.keys())
        n_rows = len(values[keys[0]]) if keys else 0
        return [{k: values[k][i] for k in keys} for i in range(n_rows)]
    return list(values)


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

    def __init__(self, period: int = 14, n: int | None = None) -> None:
        p = n if n is not None else period
        if p <= 0:
            raise ValueError(f"SMA period must be a positive integer, got {p}")
        self.period = p
        self.spec = IndicatorSpec(
            name="sma",
            inputs=("close",),
            params_schema={"n": p},
            outputs=("value",),
            warmup=p - 1,
        )

    def new_state(self) -> SMAState:
        return SMAState(period=self.period)

    def update(self, state: object, value: Any) -> float | None:
        if not isinstance(state, SMAState):
            raise TypeError(f"Expected SMAState, got {type(state).__name__}")
        val = _extract_field(value, "close")
        state.buffer.append(val)
        state.running_sum += val
        if len(state.buffer) < state.period:
            return None
        if len(state.buffer) > state.period:
            removed = state.buffer.pop(0)
            state.running_sum -= removed
        return state.running_sum / state.period

    def compute(
        self, values: Sequence[Any] | Mapping[str, Sequence[Any]]
    ) -> Sequence[float | None]:
        st = self.new_state()
        return [self.update(st, row) for row in _extract_rows(values)]


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

    def __init__(self, period: int = 14, n: int | None = None) -> None:
        p = n if n is not None else period
        if p <= 0:
            raise ValueError(f"EMA period must be a positive integer, got {p}")
        self.period = p
        self.alpha = 2.0 / (p + 1.0)
        self.spec = IndicatorSpec(
            name="ema",
            inputs=("close",),
            params_schema={"n": p},
            outputs=("value",),
            warmup=p - 1,
        )

    def new_state(self) -> EMAState:
        return EMAState(period=self.period, alpha=self.alpha)

    def update(self, state: object, value: Any) -> float | None:
        if not isinstance(state, EMAState):
            raise TypeError(f"Expected EMAState, got {type(state).__name__}")
        val = _extract_field(value, "close")
        if state.prev_ema is None:
            state.buffer.append(val)
            if len(state.buffer) < state.period:
                return None
            initial_sma = sum(state.buffer) / state.period
            state.prev_ema = initial_sma
            return initial_sma
        new_ema = (state.alpha * val) + ((1.0 - state.alpha) * state.prev_ema)
        state.prev_ema = new_ema
        return new_ema

    def compute(
        self, values: Sequence[Any] | Mapping[str, Sequence[Any]]
    ) -> Sequence[float | None]:
        st = self.new_state()
        return [self.update(st, row) for row in _extract_rows(values)]


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

    def __init__(self, period: int = 14, n: int | None = None) -> None:
        p = n if n is not None else period
        if p <= 0:
            raise ValueError(f"RSI period must be a positive integer, got {p}")
        self.period = p
        self.spec = IndicatorSpec(
            name="rsi",
            inputs=("close",),
            params_schema={"n": p},
            outputs=("value",),
            warmup=p,
        )

    def new_state(self) -> RSIState:
        return RSIState(period=self.period)

    def update(self, state: object, value: Any) -> float | None:
        if not isinstance(state, RSIState):
            raise TypeError(f"Expected RSIState, got {type(state).__name__}")
        val = _extract_field(value, "close")
        if state.prev_price is None:
            state.prev_price = val
            return None

        delta = val - state.prev_price
        state.prev_price = val
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)

        if state.avg_gain is None or state.avg_loss is None:
            state.gains.append(gain)
            state.losses.append(loss)
            if len(state.gains) < state.period:
                return None
            state.avg_gain = sum(state.gains) / state.period
            state.avg_loss = sum(state.losses) / state.period
        else:
            state.avg_gain = ((state.avg_gain * (state.period - 1)) + gain) / state.period
            state.avg_loss = ((state.avg_loss * (state.period - 1)) + loss) / state.period

        if state.avg_loss == 0.0:
            return 100.0 if state.avg_gain > 0.0 else 50.0
        if state.avg_gain == 0.0:
            return 0.0

        rs = state.avg_gain / state.avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def compute(
        self, values: Sequence[Any] | Mapping[str, Sequence[Any]]
    ) -> Sequence[float | None]:
        st = self.new_state()
        return [self.update(st, row) for row in _extract_rows(values)]


# ------------------------------------------------------------------------------
# Average True Range (ATR - Wilder)
# ------------------------------------------------------------------------------


@dataclass(slots=True)
class ATRState:
    """Incremental state for Wilder's Average True Range."""

    n: int
    prev_close: float | None = None
    tr_buffer: list[float] = field(default_factory=list)
    prev_atr: float | None = None


class ATR:
    """Wilder's Average True Range seeded with arithmetic mean of first n true ranges."""

    def __init__(self, n: int = 14, period: int | None = None) -> None:
        p = period if period is not None else n
        if p <= 0:
            raise ValueError(f"ATR period must be a positive integer, got {p}")
        self.n = p
        self.spec = IndicatorSpec(
            name="atr",
            inputs=("high", "low", "close"),
            params_schema={"n": p},
            outputs=("atr",),
            warmup=p - 1,
        )

    def new_state(self) -> ATRState:
        return ATRState(n=self.n)

    def update(self, state: object, value: Any) -> float | None:
        if not isinstance(state, ATRState):
            raise TypeError(f"Expected ATRState, got {type(state).__name__}")
        high = _extract_field(value, "high")
        low = _extract_field(value, "low")
        close = _extract_field(value, "close")

        if state.prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - state.prev_close), abs(low - state.prev_close))
        state.prev_close = close

        if state.prev_atr is None:
            state.tr_buffer.append(tr)
            if len(state.tr_buffer) < state.n:
                return None
            initial_atr = sum(state.tr_buffer) / state.n
            state.prev_atr = initial_atr
            return initial_atr

        new_atr = ((state.prev_atr * (state.n - 1)) + tr) / state.n
        state.prev_atr = new_atr
        return new_atr

    def compute(
        self, values: Sequence[Any] | Mapping[str, Sequence[Any]]
    ) -> Sequence[float | None]:
        st = self.new_state()
        return [self.update(st, row) for row in _extract_rows(values)]


# ------------------------------------------------------------------------------
# Volume Weighted Average Price (VWAP)
# ------------------------------------------------------------------------------


@dataclass(slots=True)
class VWAPState:
    """Incremental state for session-reset Volume Weighted Average Price."""

    cum_pv: float = 0.0
    cum_vol: float = 0.0
    prev_date: Any = None


class VWAP:
    """Session-reset cumulative typical price x volume over cumulative volume."""

    def __init__(self) -> None:
        self.spec = IndicatorSpec(
            name="vwap",
            inputs=("high", "low", "close", "volume", "new_session"),
            params_schema={},
            outputs=("vwap",),
            warmup=0,
            timeframes=("intraday",),
        )

    def new_state(self) -> VWAPState:
        return VWAPState()

    def update(self, state: object, value: Any) -> float | None:
        if not isinstance(state, VWAPState):
            raise TypeError(f"Expected VWAPState, got {type(state).__name__}")
        high = _extract_field(value, "high")
        low = _extract_field(value, "low")
        close = _extract_field(value, "close")
        vol = _extract_field(value, "volume")

        # Determine if new session triggered
        is_new = False
        if isinstance(value, Mapping) and "new_session" in value:
            is_new = bool(value["new_session"])
        elif hasattr(value, "new_session"):
            is_new = bool(value.new_session)
        elif hasattr(value, "start_ts") and value.start_ts is not None:
            ts = value.start_ts
            ts_ist = ts.astimezone(_IST) if ts.tzinfo else ts.replace(tzinfo=UTC).astimezone(_IST)
            cur_date = ts_ist.date()
            if state.prev_date is not None and cur_date != state.prev_date:
                is_new = True
            state.prev_date = cur_date

        if is_new:
            state.cum_pv = 0.0
            state.cum_vol = 0.0

        typical_price = (high + low + close) / 3.0
        state.cum_pv += typical_price * vol
        state.cum_vol += vol

        if state.cum_vol <= 0.0:
            return None
        return state.cum_pv / state.cum_vol

    def compute(
        self, values: Sequence[Any] | Mapping[str, Sequence[Any]]
    ) -> Sequence[float | None]:
        st = self.new_state()
        return [self.update(st, row) for row in _extract_rows(values)]


# ------------------------------------------------------------------------------
# Bollinger Bands
# ------------------------------------------------------------------------------


@dataclass(slots=True)
class BollingerState:
    """Incremental state for Bollinger Bands."""

    n: int
    k: float
    buffer: list[float] = field(default_factory=list)
    running_sum: float = 0.0
    running_sq_sum: float = 0.0


class Bollinger:
    """Bollinger Bands: middle=SMA(n), upper/lower = middle +- k x pop std deviation."""

    def __init__(
        self,
        n: int = 20,
        k: float = 2.0,
        period: int | None = None,
        std_dev: float | None = None,
    ) -> None:
        p = period if period is not None else n
        mult = std_dev if std_dev is not None else k
        if p <= 0:
            raise ValueError(f"Bollinger period n must be a positive integer, got {p}")
        if mult <= 0.0:
            raise ValueError(f"Bollinger multiplier k must be positive, got {mult}")
        self.n = p
        self.k = mult
        self.spec = IndicatorSpec(
            name="bollinger",
            inputs=("close",),
            params_schema={"n": p, "k": mult},
            outputs=("middle", "upper", "lower"),
            warmup=p - 1,
        )

    def new_state(self) -> BollingerState:
        return BollingerState(n=self.n, k=self.k)

    def update(self, state: object, value: Any) -> dict[str, float | None]:
        if not isinstance(state, BollingerState):
            raise TypeError(f"Expected BollingerState, got {type(state).__name__}")
        val = _extract_field(value, "close")
        state.buffer.append(val)
        state.running_sum += val
        state.running_sq_sum += val * val

        if len(state.buffer) < state.n:
            return {"middle": None, "upper": None, "lower": None}
        if len(state.buffer) > state.n:
            removed = state.buffer.pop(0)
            state.running_sum -= removed
            state.running_sq_sum -= removed * removed

        middle = state.running_sum / state.n
        # Population standard deviation over n (divide by N)
        variance = max(0.0, (state.running_sq_sum / state.n) - (middle * middle))
        std = math.sqrt(variance)
        upper = middle + (state.k * std)
        lower = middle - (state.k * std)
        return {"middle": middle, "upper": upper, "lower": lower}

    def compute(
        self, values: Sequence[Any] | Mapping[str, Sequence[Any]]
    ) -> dict[str, Sequence[float | None]]:
        st = self.new_state()
        rows = [self.update(st, row) for row in _extract_rows(values)]
        return {
            "middle": [r["middle"] for r in rows],
            "upper": [r["upper"] for r in rows],
            "lower": [r["lower"] for r in rows],
        }


# ------------------------------------------------------------------------------
# Moving Average Convergence Divergence (MACD)
# ------------------------------------------------------------------------------


@dataclass(slots=True)
class MACDState:
    """Incremental state for MACD."""

    fast_ema: EMA
    slow_ema: EMA
    signal_ema: EMA
    fast_state: EMAState
    slow_state: EMAState
    signal_state: EMAState


class MACD:
    """Moving Average Convergence Divergence with fast/slow EMA and signal line."""

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9) -> None:
        if fast <= 0:
            raise ValueError(f"MACD fast must be positive, got {fast}")
        if slow <= 0:
            raise ValueError(f"MACD slow must be positive, got {slow}")
        if fast >= slow:
            raise ValueError(f"MACD fast ({fast}) must be less than slow ({slow})")
        if signal <= 0:
            raise ValueError(f"MACD signal must be positive, got {signal}")
        self.fast = fast
        self.slow = slow
        self.signal = signal
        self.spec = IndicatorSpec(
            name="macd",
            inputs=("close",),
            params_schema={"fast": fast, "slow": slow, "signal": signal},
            outputs=("macd", "signal", "histogram"),
            warmup=slow + signal - 2,
        )

    def new_state(self) -> MACDState:
        f_ema = EMA(n=self.fast)
        s_ema = EMA(n=self.slow)
        sig_ema = EMA(n=self.signal)
        return MACDState(
            fast_ema=f_ema,
            slow_ema=s_ema,
            signal_ema=sig_ema,
            fast_state=f_ema.new_state(),
            slow_state=s_ema.new_state(),
            signal_state=sig_ema.new_state(),
        )

    def update(self, state: object, value: Any) -> dict[str, float | None]:
        if not isinstance(state, MACDState):
            raise TypeError(f"Expected MACDState, got {type(state).__name__}")
        val = _extract_field(value, "close")
        f_val = state.fast_ema.update(state.fast_state, val)
        s_val = state.slow_ema.update(state.slow_state, val)

        if f_val is not None and s_val is not None:
            macd_val = f_val - s_val
            sig_val = state.signal_ema.update(state.signal_state, macd_val)
            hist_val = macd_val - sig_val if sig_val is not None else None
            return {"macd": macd_val, "signal": sig_val, "histogram": hist_val}

        return {"macd": None, "signal": None, "histogram": None}

    def compute(
        self, values: Sequence[Any] | Mapping[str, Sequence[Any]]
    ) -> dict[str, Sequence[float | None]]:
        st = self.new_state()
        rows = [self.update(st, row) for row in _extract_rows(values)]
        return {
            "macd": [r["macd"] for r in rows],
            "signal": [r["signal"] for r in rows],
            "histogram": [r["histogram"] for r in rows],
        }


# ------------------------------------------------------------------------------
# Rolling High / Low (RollingHL)
# ------------------------------------------------------------------------------


@dataclass(slots=True)
class RollingHLState:
    """Incremental state for Rolling High / Low."""

    n: int
    high_buffer: list[float] = field(default_factory=list)
    low_buffer: list[float] = field(default_factory=list)


class RollingHL:
    """Rolling highest high and lowest low over n completed bars including current."""

    def __init__(self, n: int = 20, period: int | None = None) -> None:
        p = period if period is not None else n
        if p <= 0:
            raise ValueError(f"RollingHL period n must be positive, got {p}")
        self.n = p
        self.spec = IndicatorSpec(
            name="rolling_hl",
            inputs=("high", "low"),
            params_schema={"n": p},
            outputs=("high", "low"),
            warmup=p - 1,
        )

    def new_state(self) -> RollingHLState:
        return RollingHLState(n=self.n)

    def update(self, state: object, value: Any) -> dict[str, float | None]:
        if not isinstance(state, RollingHLState):
            raise TypeError(f"Expected RollingHLState, got {type(state).__name__}")
        high = _extract_field(value, "high")
        low = _extract_field(value, "low")
        state.high_buffer.append(high)
        state.low_buffer.append(low)
        if len(state.high_buffer) < state.n:
            return {"high": None, "low": None}
        if len(state.high_buffer) > state.n:
            state.high_buffer.pop(0)
            state.low_buffer.pop(0)
        return {"high": max(state.high_buffer), "low": min(state.low_buffer)}

    def compute(
        self, values: Sequence[Any] | Mapping[str, Sequence[Any]]
    ) -> dict[str, Sequence[float | None]]:
        st = self.new_state()
        rows = [self.update(st, row) for row in _extract_rows(values)]
        return {
            "high": [r["high"] for r in rows],
            "low": [r["low"] for r in rows],
        }


# ------------------------------------------------------------------------------
# Volume Average (VolumeAvg)
# ------------------------------------------------------------------------------


@dataclass(slots=True)
class VolumeAvgState:
    """Incremental state for Volume Average."""

    n: int
    buffer: list[float] = field(default_factory=list)
    running_sum: float = 0.0


class VolumeAvg:
    """Simple moving average of volume over rolling window of n bars."""

    def __init__(self, n: int = 20, period: int | None = None) -> None:
        p = period if period is not None else n
        if p <= 0:
            raise ValueError(f"VolumeAvg period n must be positive, got {p}")
        self.n = p
        self.spec = IndicatorSpec(
            name="volume_avg",
            inputs=("volume",),
            params_schema={"n": p},
            outputs=("volume_avg",),
            warmup=p - 1,
        )

    def new_state(self) -> VolumeAvgState:
        return VolumeAvgState(n=self.n)

    def update(self, state: object, value: Any) -> float | None:
        if not isinstance(state, VolumeAvgState):
            raise TypeError(f"Expected VolumeAvgState, got {type(state).__name__}")
        vol = _extract_field(value, "volume")
        state.buffer.append(vol)
        state.running_sum += vol
        if len(state.buffer) < state.n:
            return None
        if len(state.buffer) > state.n:
            state.running_sum -= state.buffer.pop(0)
        return state.running_sum / state.n

    def compute(
        self, values: Sequence[Any] | Mapping[str, Sequence[Any]]
    ) -> Sequence[float | None]:
        st = self.new_state()
        return [self.update(st, row) for row in _extract_rows(values)]


# ------------------------------------------------------------------------------
# Rate of Change (ROC)
# ------------------------------------------------------------------------------


@dataclass(slots=True)
class ROCState:
    """Incremental state for Rate of Change."""

    n: int
    buffer: list[float] = field(default_factory=list)


class ROC:
    """Rate of Change: close / close n bars earlier minus 1, as a fraction."""

    def __init__(self, n: int = 12, period: int | None = None) -> None:
        p = period if period is not None else n
        if p <= 0:
            raise ValueError(f"ROC period n must be positive, got {p}")
        self.n = p
        self.spec = IndicatorSpec(
            name="roc",
            inputs=("close",),
            params_schema={"n": p},
            outputs=("roc",),
            warmup=p,
        )

    def new_state(self) -> ROCState:
        return ROCState(n=self.n)

    def update(self, state: object, value: Any) -> float | None:
        if not isinstance(state, ROCState):
            raise TypeError(f"Expected ROCState, got {type(state).__name__}")
        val = _extract_field(value, "close")
        state.buffer.append(val)
        if len(state.buffer) <= state.n:
            return None
        earlier = state.buffer.pop(0)
        if earlier == 0.0:
            return None
        return (val / earlier) - 1.0

    def compute(
        self, values: Sequence[Any] | Mapping[str, Sequence[Any]]
    ) -> Sequence[float | None]:
        st = self.new_state()
        return [self.update(st, row) for row in _extract_rows(values)]


__all__ = [
    "ATR",
    "EMA",
    "MACD",
    "ROC",
    "RSI",
    "SMA",
    "VWAP",
    "Bollinger",
    "RollingHL",
    "VolumeAvg",
]
