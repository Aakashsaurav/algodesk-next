"""Unit tests and golden vector validations for builtin indicators."""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from algodesk.api.manifest import PluginManifest

from ..indicators import (
    ATR,
    EMA,
    MACD,
    ROC,
    RSI,
    SMA,
    VWAP,
    Bollinger,
    RollingHL,
    VolumeAvg,
)
from ..plugin import BuiltinIndicatorsPlugin

# ------------------------------------------------------------------------------
# Independent Reference Implementations
# ------------------------------------------------------------------------------


def _ref_sma(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = []
    for i in range(len(values)):
        if i < period - 1:
            result.append(None)
        else:
            window = values[i - period + 1 : i + 1]
            result.append(sum(window) / period)
    return result


def _ref_ema(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = []
    alpha = 2.0 / (period + 1.0)
    current_ema: float | None = None
    for i in range(len(values)):
        if i < period - 1:
            result.append(None)
        elif i == period - 1:
            current_ema = sum(values[:period]) / period
            result.append(current_ema)
        else:
            assert current_ema is not None
            current_ema = (alpha * values[i]) + ((1.0 - alpha) * current_ema)
            result.append(current_ema)
    return result


def _ref_rsi(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = []
    gains: list[float] = []
    losses: list[float] = []
    avg_gain: float | None = None
    avg_loss: float | None = None

    for i in range(len(values)):
        if i == 0:
            result.append(None)
            continue

        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)

        if avg_gain is None or avg_loss is None:
            gains.append(gain)
            losses.append(loss)
            if len(gains) < period:
                result.append(None)
            else:
                avg_gain = sum(gains) / period
                avg_loss = sum(losses) / period
                if avg_loss == 0.0:
                    result.append(100.0 if avg_gain > 0.0 else 50.0)
                elif avg_gain == 0.0:
                    result.append(0.0)
                else:
                    rs = avg_gain / avg_loss
                    result.append(100.0 - (100.0 / (1.0 + rs)))
        else:
            avg_gain = ((avg_gain * (period - 1)) + gain) / period
            avg_loss = ((avg_loss * (period - 1)) + loss) / period
            if avg_loss == 0.0:
                result.append(100.0 if avg_gain > 0.0 else 50.0)
            elif avg_gain == 0.0:
                result.append(0.0)
            else:
                rs = avg_gain / avg_loss
                result.append(100.0 - (100.0 / (1.0 + rs)))

    return result


def _ref_atr(high: list[float], low: list[float], close: list[float], n: int) -> list[float | None]:
    trs: list[float] = []
    res: list[float | None] = []
    prev_atr: float | None = None
    for i in range(len(close)):
        if i == 0:
            tr = high[0] - low[0]
        else:
            tr = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
        trs.append(tr)
        if i < n - 1:
            res.append(None)
        elif i == n - 1:
            prev_atr = sum(trs[:n]) / n
            res.append(prev_atr)
        else:
            assert prev_atr is not None
            prev_atr = ((prev_atr * (n - 1)) + tr) / n
            res.append(prev_atr)
    return res


def _ref_vwap(
    high: list[float], low: list[float], close: list[float], volume: list[float]
) -> list[float | None]:
    res: list[float | None] = []
    cum_pv = 0.0
    cum_vol = 0.0
    for h, lo, c, v in zip(high, low, close, volume, strict=True):
        tp = (h + lo + c) / 3.0
        cum_pv += tp * v
        cum_vol += v
        res.append(cum_pv / cum_vol if cum_vol > 0 else None)
    return res


def _ref_bollinger(close: list[float], n: int, k: float) -> dict[str, list[float | None]]:
    mids: list[float | None] = []
    uppers: list[float | None] = []
    lowers: list[float | None] = []
    for i in range(len(close)):
        if i < n - 1:
            mids.append(None)
            uppers.append(None)
            lowers.append(None)
        else:
            win = close[i - n + 1 : i + 1]
            m = sum(win) / n
            var = sum((x - m) ** 2 for x in win) / n
            std = math.sqrt(var)
            mids.append(m)
            uppers.append(m + k * std)
            lowers.append(m - k * std)
    return {"middle": mids, "upper": uppers, "lower": lowers}


def _ref_macd(
    close: list[float], fast: int, slow: int, signal: int
) -> dict[str, list[float | None]]:
    ema_fast = _ref_ema(close, fast)
    ema_slow = _ref_ema(close, slow)
    macd_line: list[float | None] = []
    for f, s in zip(ema_fast, ema_slow, strict=True):
        if f is None or s is None:
            macd_line.append(None)
        else:
            macd_line.append(f - s)

    valid_macd = [m for m in macd_line if m is not None]
    sig_valid = _ref_ema(valid_macd, signal)
    signal_line: list[float | None] = []
    histogram: list[float | None] = []
    sig_idx = 0
    for m in macd_line:
        if m is None:
            signal_line.append(None)
            histogram.append(None)
        else:
            s_val = sig_valid[sig_idx]
            sig_idx += 1
            signal_line.append(s_val)
            histogram.append((m - s_val) if s_val is not None else None)
    return {"macd": macd_line, "signal": signal_line, "histogram": histogram}


def _ref_rolling_hl(high: list[float], low: list[float], n: int) -> dict[str, list[float | None]]:
    highs: list[float | None] = []
    lows: list[float | None] = []
    for i in range(len(high)):
        if i < n - 1:
            highs.append(None)
            lows.append(None)
        else:
            highs.append(max(high[i - n + 1 : i + 1]))
            lows.append(min(low[i - n + 1 : i + 1]))
    return {"high": highs, "low": lows}


def _ref_volume_avg(volume: list[float], n: int) -> list[float | None]:
    return _ref_sma(volume, n)


def _ref_roc(close: list[float], n: int) -> list[float | None]:
    res: list[float | None] = []
    for i in range(len(close)):
        if i < n:
            res.append(None)
        else:
            res.append((close[i] / close[i - n]) - 1.0)
    return res


# ------------------------------------------------------------------------------
# Golden Vector Tests
# ------------------------------------------------------------------------------


def test_sma_golden_vectors() -> None:
    # Vector 1
    v1 = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0]
    expected1 = [None, None, 11.0, 12.0, 13.0, 14.0, 15.0]
    assert SMA(period=3).compute(v1) == expected1
    # Vector 2
    v2 = [100.0, 200.0, 300.0, 400.0]
    expected2 = [None, 150.0, 250.0, 350.0]
    assert SMA(period=2).compute(v2) == expected2


def test_ema_golden_vectors() -> None:
    # Vector 1
    v1 = [10.0, 20.0, 30.0, 40.0, 50.0]
    expected1 = [None, None, 20.0, 30.0, 40.0]
    assert EMA(period=3).compute(v1) == expected1
    # Vector 2
    v2 = [10.0, 20.0, 40.0]
    # seed = 15.0 at idx 1. alpha = 2/3.
    # idx 2: 40 * 2/3 + 15 * 1/3 = 80/3 + 15/3 = 95/3 = 31.666667
    res2 = EMA(period=2).compute(v2)
    assert res2[0] is None
    assert res2[1] == 15.0
    assert res2[2] is not None and math.isclose(res2[2], 95.0 / 3.0)


def test_rsi_golden_vectors() -> None:
    v1 = [10.0, 12.0, 11.0, 13.0, 15.0]
    res1 = RSI(period=3).compute(v1)
    assert res1[0] is None and res1[1] is None and res1[2] is None
    assert res1[3] is not None and math.isclose(res1[3], 80.0, abs_tol=1e-5)
    assert res1[4] is not None and math.isclose(res1[4], 87.5, abs_tol=1e-5)

    v2 = [50.0, 40.0, 30.0, 20.0]
    res2 = RSI(period=2).compute(v2)
    assert res2[0] is None and res2[1] is None
    assert res2[2] == 0.0
    assert res2[3] == 0.0


def test_atr_golden_vectors() -> None:
    h1 = [12.0, 14.0, 13.0, 15.0, 16.0]
    l1 = [10.0, 11.0, 10.0, 12.0, 13.0]
    c1 = [11.0, 13.0, 12.0, 14.0, 15.0]
    atr = ATR(n=3)
    res1 = atr.compute({"high": h1, "low": l1, "close": c1})
    assert res1[0] is None and res1[1] is None
    assert res1[2] is not None and math.isclose(res1[2], 8.0 / 3.0, abs_tol=1e-5)
    assert res1[3] is not None and math.isclose(res1[3], 2.777778, abs_tol=1e-5)

    # Vector 2 (flat prices, TR = H - L)
    h2 = [10.0, 10.0, 10.0]
    l2 = [8.0, 8.0, 8.0]
    c2 = [9.0, 9.0, 9.0]
    res2 = ATR(n=2).compute({"high": h2, "low": l2, "close": c2})
    assert res2 == [None, 2.0, 2.0]


def test_vwap_golden_vectors() -> None:
    h = [12.0, 14.0, 15.0]
    low_vals = [10.0, 12.0, 13.0]
    c = [11.0, 13.0, 14.0]
    v = [100.0, 200.0, 100.0]
    vwap = VWAP()
    res1 = vwap.compute({"high": h, "low": low_vals, "close": c, "volume": v})
    assert res1[0] == 11.0
    assert res1[1] is not None and math.isclose(res1[1], 12.333333, abs_tol=1e-5)
    assert res1[2] == 12.75

    # Zero volume produces None
    res2 = vwap.compute({"high": [10.0], "low": [10.0], "close": [10.0], "volume": [0.0]})
    assert res2 == [None]


def test_bollinger_golden_vectors() -> None:
    data = [10.0, 20.0, 30.0, 40.0, 50.0]
    b = Bollinger(n=3, k=2.0)
    res = b.compute(data)
    assert res["middle"][:2] == [None, None]
    assert res["middle"][2] == 20.0
    assert res["middle"][3] == 30.0
    assert res["middle"][4] == 40.0
    # sigma = sqrt(200/3) = 8.1649658
    assert res["upper"][2] is not None and math.isclose(
        res["upper"][2], 20.0 + 2.0 * math.sqrt(200.0 / 3.0)
    )
    assert res["lower"][2] is not None and math.isclose(
        res["lower"][2], 20.0 - 2.0 * math.sqrt(200.0 / 3.0)
    )

    # Flat data -> upper == lower == middle
    res_flat = Bollinger(n=2, k=2.0).compute([5.0, 5.0, 5.0])
    assert res_flat["middle"] == [None, 5.0, 5.0]
    assert res_flat["upper"] == [None, 5.0, 5.0]
    assert res_flat["lower"] == [None, 5.0, 5.0]


def test_macd_golden_vectors() -> None:
    prices = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0]
    macd = MACD(fast=2, slow=4, signal=2)
    res = macd.compute(prices)
    assert res["macd"][:3] == [None, None, None]
    assert res["signal"][:4] == [None, None, None, None]
    assert res["histogram"][:4] == [None, None, None, None]
    assert res["macd"][3] == 1.0
    assert res["signal"][4] == 1.0
    assert res["histogram"][4] == 0.0


def test_rolling_hl_golden_vectors() -> None:
    h = [12.0, 15.0, 14.0, 16.0, 13.0]
    low_vals = [10.0, 11.0, 9.0, 12.0, 11.0]
    rhl = RollingHL(n=3)
    res = rhl.compute({"high": h, "low": low_vals})
    assert res["high"] == [None, None, 15.0, 16.0, 16.0]
    assert res["low"] == [None, None, 9.0, 9.0, 9.0]

    # n=1
    res1 = RollingHL(n=1).compute({"high": [10.0, 12.0], "low": [9.0, 11.0]})
    assert res1["high"] == [10.0, 12.0]
    assert res1["low"] == [9.0, 11.0]


def test_volume_avg_golden_vectors() -> None:
    vol = [100.0, 200.0, 300.0, 400.0, 500.0]
    vavg = VolumeAvg(n=3)
    res = vavg.compute(vol)
    assert res == [None, None, 200.0, 300.0, 400.0]

    res2 = VolumeAvg(n=2).compute([10.0, 20.0])
    assert res2 == [None, 15.0]


def test_roc_golden_vectors() -> None:
    prices = [100.0, 105.0, 110.0, 115.5]
    roc = ROC(n=2)
    res = roc.compute(prices)
    assert res[0] is None and res[1] is None
    assert res[2] is not None and math.isclose(res[2], 0.10, abs_tol=1e-5)
    assert res[3] is not None and math.isclose(res[3], 0.10, abs_tol=1e-5)

    res1 = ROC(n=1).compute([100.0, 110.0, 99.0])
    assert res1[0] is None
    assert res1[1] is not None and math.isclose(res1[1], 0.10)
    assert res1[2] is not None and math.isclose(res1[2], -0.10)


# ------------------------------------------------------------------------------
# Hypothesis Property Tests (Batch == Incremental)
# ------------------------------------------------------------------------------


@given(
    st.lists(
        st.floats(min_value=-10000.0, max_value=10000.0, allow_nan=False),
        min_size=1,
        max_size=30,
    )
)
def test_sma_hypothesis(data: list[float]) -> None:
    sma = SMA(period=3)
    batch = sma.compute(data)
    st_obj = sma.new_state()
    inc = [sma.update(st_obj, v) for v in data]
    assert len(batch) == len(inc)
    for b, i in zip(batch, inc, strict=True):
        if b is None:
            assert i is None
        else:
            assert i is not None and math.isclose(b, i, rel_tol=1e-7, abs_tol=1e-7)


@given(
    st.lists(
        st.floats(min_value=-10000.0, max_value=10000.0, allow_nan=False),
        min_size=1,
        max_size=30,
    )
)
def test_ema_hypothesis(data: list[float]) -> None:
    ema = EMA(period=3)
    batch = ema.compute(data)
    st_obj = ema.new_state()
    inc = [ema.update(st_obj, v) for v in data]
    assert len(batch) == len(inc)
    for b, i in zip(batch, inc, strict=True):
        if b is None:
            assert i is None
        else:
            assert i is not None and math.isclose(b, i, rel_tol=1e-7, abs_tol=1e-7)


@given(
    st.lists(
        st.floats(min_value=1.0, max_value=10000.0, allow_nan=False),
        min_size=1,
        max_size=30,
    )
)
def test_rsi_hypothesis(data: list[float]) -> None:
    rsi = RSI(period=3)
    batch = rsi.compute(data)
    st_obj = rsi.new_state()
    inc = [rsi.update(st_obj, v) for v in data]
    assert len(batch) == len(inc)
    for b, i in zip(batch, inc, strict=True):
        if b is None:
            assert i is None
        else:
            assert i is not None and math.isclose(b, i, rel_tol=1e-6, abs_tol=1e-6)


@given(
    st.lists(
        st.tuples(
            st.floats(min_value=10.0, max_value=100.0, allow_nan=False),
            st.floats(min_value=1.0, max_value=9.0, allow_nan=False),
            st.floats(min_value=5.0, max_value=10.0, allow_nan=False),
        ),
        min_size=1,
        max_size=30,
    )
)
def test_atr_hypothesis(data: list[tuple[float, float, float]]) -> None:
    high = [max(h, lo, c) for h, lo, c in data]
    low = [min(h, lo, c) for h, lo, c in data]
    close = [c for _, _, c in data]
    inputs = {"high": high, "low": low, "close": close}

    atr = ATR(n=3)
    batch = atr.compute(inputs)
    st_obj = atr.new_state()
    inc = [
        atr.update(st_obj, {"high": high[i], "low": low[i], "close": close[i]})
        for i in range(len(data))
    ]
    assert len(batch) == len(inc)
    for b, i in zip(batch, inc, strict=True):
        if b is None:
            assert i is None
        else:
            assert i is not None and math.isclose(b, i, rel_tol=1e-6, abs_tol=1e-6)


@given(
    st.lists(
        st.tuples(
            st.floats(min_value=10.0, max_value=100.0, allow_nan=False),
            st.floats(min_value=1.0, max_value=9.0, allow_nan=False),
            st.floats(min_value=5.0, max_value=10.0, allow_nan=False),
            st.floats(min_value=1.0, max_value=1000.0, allow_nan=False),
        ),
        min_size=1,
        max_size=30,
    )
)
def test_vwap_hypothesis(data: list[tuple[float, float, float, float]]) -> None:
    high = [max(h, lo, c) for h, lo, c, _ in data]
    low = [min(h, lo, c) for h, lo, c, _ in data]
    close = [c for _, _, c, _ in data]
    vol = [v for _, _, _, v in data]
    inputs = {"high": high, "low": low, "close": close, "volume": vol}

    vwap = VWAP()
    batch = vwap.compute(inputs)
    st_obj = vwap.new_state()
    inc = [
        vwap.update(st_obj, {"high": high[i], "low": low[i], "close": close[i], "volume": vol[i]})
        for i in range(len(data))
    ]
    assert len(batch) == len(inc)
    for b, i in zip(batch, inc, strict=True):
        if b is None:
            assert i is None
        else:
            assert i is not None and math.isclose(b, i, rel_tol=1e-6, abs_tol=1e-6)


@given(
    st.lists(
        st.floats(min_value=1.0, max_value=1000.0, allow_nan=False),
        min_size=1,
        max_size=30,
    )
)
def test_bollinger_hypothesis(data: list[float]) -> None:
    b = Bollinger(n=3, k=2.0)
    batch = b.compute(data)
    st_obj = b.new_state()
    inc = [b.update(st_obj, v) for v in data]
    for key in ("middle", "upper", "lower"):
        b_series = batch[key]
        i_series = [r[key] for r in inc]
        assert len(b_series) == len(i_series)
        for bv, iv in zip(b_series, i_series, strict=True):
            if bv is None:
                assert iv is None
            else:
                assert iv is not None and math.isclose(bv, iv, rel_tol=1e-6, abs_tol=1e-6)


@given(
    st.lists(
        st.floats(min_value=10.0, max_value=1000.0, allow_nan=False),
        min_size=1,
        max_size=30,
    )
)
def test_macd_hypothesis(data: list[float]) -> None:
    macd = MACD(fast=2, slow=4, signal=2)
    batch = macd.compute(data)
    st_obj = macd.new_state()
    inc = [macd.update(st_obj, v) for v in data]
    for key in ("macd", "signal", "histogram"):
        b_series = batch[key]
        i_series = [r[key] for r in inc]
        assert len(b_series) == len(i_series)
        for bv, iv in zip(b_series, i_series, strict=True):
            if bv is None:
                assert iv is None
            else:
                assert iv is not None and math.isclose(bv, iv, rel_tol=1e-6, abs_tol=1e-6)


@given(
    st.lists(
        st.tuples(
            st.floats(min_value=10.0, max_value=100.0, allow_nan=False),
            st.floats(min_value=1.0, max_value=9.0, allow_nan=False),
        ),
        min_size=1,
        max_size=30,
    )
)
def test_rolling_hl_hypothesis(data: list[tuple[float, float]]) -> None:
    high = [max(h, lo) for h, lo in data]
    low = [min(h, lo) for h, lo in data]
    inputs = {"high": high, "low": low}

    rhl = RollingHL(n=3)
    batch = rhl.compute(inputs)
    st_obj = rhl.new_state()
    inc = [rhl.update(st_obj, {"high": high[i], "low": low[i]}) for i in range(len(data))]
    for key in ("high", "low"):
        b_s = batch[key]
        i_s = [r[key] for r in inc]
        assert len(b_s) == len(i_s)
        for bv, iv in zip(b_s, i_s, strict=True):
            if bv is None:
                assert iv is None
            else:
                assert iv is not None and math.isclose(bv, iv, rel_tol=1e-6, abs_tol=1e-6)


@given(
    st.lists(
        st.floats(min_value=0.0, max_value=1000000.0, allow_nan=False),
        min_size=1,
        max_size=30,
    )
)
def test_volume_avg_hypothesis(data: list[float]) -> None:
    vavg = VolumeAvg(n=3)
    batch = vavg.compute(data)
    st_obj = vavg.new_state()
    inc = [vavg.update(st_obj, v) for v in data]
    assert len(batch) == len(inc)
    for b, i in zip(batch, inc, strict=True):
        if b is None:
            assert i is None
        else:
            assert i is not None and math.isclose(b, i, rel_tol=1e-7, abs_tol=1e-7)


@given(
    st.lists(
        st.floats(min_value=1.0, max_value=10000.0, allow_nan=False),
        min_size=1,
        max_size=30,
    )
)
def test_roc_hypothesis(data: list[float]) -> None:
    roc = ROC(n=2)
    batch = roc.compute(data)
    st_obj = roc.new_state()
    inc = [roc.update(st_obj, v) for v in data]
    assert len(batch) == len(inc)
    for b, i in zip(batch, inc, strict=True):
        if b is None:
            assert i is None
        else:
            assert i is not None and math.isclose(b, i, rel_tol=1e-7, abs_tol=1e-7)


# ------------------------------------------------------------------------------
# Warm-up and Look-ahead Tests
# ------------------------------------------------------------------------------


def test_indicators_warmup_none() -> None:
    # SMA warmup 3
    assert SMA(period=4).compute([1.0, 2.0, 3.0]) == [None, None, None]
    # EMA warmup 3
    assert EMA(period=4).compute([1.0, 2.0, 3.0]) == [None, None, None]
    # RSI warmup 4
    assert RSI(period=4).compute([1.0, 2.0, 3.0, 4.0]) == [None, None, None, None]
    # ATR warmup 3
    atr_res = ATR(n=4).compute(
        {"high": [2.0, 2.0, 2.0], "low": [1.0, 1.0, 1.0], "close": [1.5, 1.5, 1.5]}
    )
    assert atr_res == [None, None, None]
    # Bollinger warmup 3
    boll_res = Bollinger(n=4, k=2.0).compute([1.0, 2.0, 3.0])
    assert boll_res["middle"] == [None, None, None]
    # MACD warmup (slow=4, signal=2 -> warmup=4)
    macd_res = MACD(fast=2, slow=4, signal=2).compute([1.0, 2.0, 3.0, 4.0])
    assert macd_res["signal"] == [None, None, None, None]
    # RollingHL warmup 3
    rhl_res = RollingHL(n=4).compute({"high": [2.0, 2.0, 2.0], "low": [1.0, 1.0, 1.0]})
    assert rhl_res["high"] == [None, None, None]
    # VolumeAvg warmup 3
    assert VolumeAvg(n=4).compute([10.0, 20.0, 30.0]) == [None, None, None]
    # ROC warmup 3
    assert ROC(n=3).compute([10.0, 20.0, 30.0]) == [None, None, None]


def test_indicators_no_lookahead() -> None:
    data = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0]
    # For every indicator, prefix computation must match full computation on that prefix
    # SMA
    sma = SMA(period=3)
    assert sma.compute(data[:5]) == sma.compute(data)[:5]
    # EMA
    ema = EMA(period=3)
    assert ema.compute(data[:5]) == ema.compute(data)[:5]
    # RSI
    rsi = RSI(period=3)
    assert rsi.compute(data[:5]) == rsi.compute(data)[:5]
    # Bollinger
    boll = Bollinger(n=3, k=2.0)
    b_short = boll.compute(data[:5])
    b_full = boll.compute(data)
    for k in ("middle", "upper", "lower"):
        assert b_short[k] == b_full[k][:5]
    # MACD
    macd = MACD(fast=2, slow=4, signal=2)
    m_short = macd.compute(data[:6])
    m_full = macd.compute(data)
    for k in ("macd", "signal", "histogram"):
        assert m_short[k] == m_full[k][:6]
    # ROC
    roc = ROC(n=2)
    assert roc.compute(data[:5]) == roc.compute(data)[:5]


# ------------------------------------------------------------------------------
# Parameter Bounds Rejection Tests
# ------------------------------------------------------------------------------


def test_parameter_bounds_rejection() -> None:
    for cls in (SMA, EMA, RSI, ATR, RollingHL, VolumeAvg, ROC):
        with pytest.raises(ValueError, match="positive"):
            cls(n=0)  # type: ignore[call-arg]
        with pytest.raises(ValueError, match="positive"):
            cls(n=-5)  # type: ignore[call-arg]

    with pytest.raises(ValueError, match="positive"):
        Bollinger(n=0, k=2.0)
    with pytest.raises(ValueError, match="positive"):
        Bollinger(n=20, k=0.0)
    with pytest.raises(ValueError, match="positive"):
        Bollinger(n=20, k=-1.0)

    with pytest.raises(ValueError, match="positive"):
        MACD(fast=0, slow=26, signal=9)
    with pytest.raises(ValueError, match="must be less than slow"):
        MACD(fast=26, slow=26, signal=9)
    with pytest.raises(ValueError, match="must be less than slow"):
        MACD(fast=30, slow=26, signal=9)
    with pytest.raises(ValueError, match="positive"):
        MACD(fast=12, slow=26, signal=0)


def test_vwap_timeframe_bounds() -> None:
    vwap = VWAP()
    assert vwap.spec.timeframes == ("intraday",)


# ------------------------------------------------------------------------------
# Plugin Conformance and Gating Tests
# ------------------------------------------------------------------------------


def test_builtin_plugin_conformance() -> None:
    manifest = PluginManifest(
        name="builtin",
        kind="indicator",  # type: ignore[arg-type]
        version="0.2.0",
        api_version=1,
        entry="plugin:BuiltinIndicatorsPlugin",
        capabilities={
            "sma": True,
            "ema": True,
            "rsi": True,
            "atr": True,
            "vwap": True,
            "bollinger": True,
            "macd": True,
            "rolling_hl": True,
            "volume_avg": True,
            "roc": True,
        },
    )
    plugin = BuiltinIndicatorsPlugin(manifest, {"indicator": "sma", "period": 5})

    assert plugin.supports("sma") is True
    assert plugin.supports("ema") is True
    assert plugin.supports("rsi") is True
    assert plugin.supports("atr") is True
    assert plugin.supports("vwap") is True
    assert plugin.supports("bollinger") is True
    assert plugin.supports("macd") is True
    assert plugin.supports("rolling_hl") is True
    assert plugin.supports("volume_avg") is True
    assert plugin.supports("roc") is True
    assert plugin.supports("stochastic") is False

    # Test factory method
    for name in (
        "sma",
        "ema",
        "rsi",
        "atr",
        "vwap",
        "bollinger",
        "macd",
        "rolling_hl",
        "volume_avg",
        "roc",
    ):
        ind = plugin.get_indicator(name)
        assert ind.spec.name == name

    with pytest.raises(ValueError, match="Unknown builtin indicator"):
        plugin.get_indicator("unknown")
