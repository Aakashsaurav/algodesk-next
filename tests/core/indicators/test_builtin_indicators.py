"""Comprehensive unit tests for all builtin technical indicators."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from plugins.indicators.builtin.indicators import (
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

from algodesk.api.market import Bar, InstrumentId


def _make_bar(
    high: float, low: float, close: float, volume: int = 100, dt: datetime | None = None
) -> Bar:
    t = dt or datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
    return Bar(
        instrument=InstrumentId("NSE:EQ:TEST"),
        timeframe="5m",
        start_ts=t,
        end_ts=t + timedelta(minutes=5),
        open=close,
        high=high,
        low=low,
        close=close,
        volume=volume,
        complete=True,
        synthetic=False,
    )


# ------------------------------------------------------------------------------
# SMA Tests
# ------------------------------------------------------------------------------


def test_sma_spec_and_warmup() -> None:
    ind = SMA(period=5)
    assert ind.spec.name == "sma"
    assert ind.spec.inputs == ("close",)
    assert ind.spec.outputs == ("value",)
    assert ind.spec.warmup == 4
    assert ind.compute([1.0, 2.0, 3.0, 4.0]) == [None, None, None, None]


def test_sma_bounds() -> None:
    with pytest.raises(ValueError, match="positive"):
        SMA(period=0)
    with pytest.raises(ValueError, match="positive"):
        SMA(period=-1)


def test_sma_incremental() -> None:
    ind = SMA(period=3)
    vals = [10.0, 20.0, 30.0, 40.0]
    st = ind.new_state()
    res = [ind.update(st, v) for v in vals]
    assert res == [None, None, 20.0, 30.0]


# ------------------------------------------------------------------------------
# EMA Tests
# ------------------------------------------------------------------------------


def test_ema_spec_and_warmup() -> None:
    ind = EMA(period=4)
    assert ind.spec.name == "ema"
    assert ind.spec.inputs == ("close",)
    assert ind.spec.outputs == ("value",)
    assert ind.spec.warmup == 3
    assert ind.compute([10.0, 20.0, 30.0]) == [None, None, None]


def test_ema_seed_and_smoothing() -> None:
    ind = EMA(period=3)
    data = [10.0, 20.0, 30.0, 40.0]
    # seed = 20.0 at idx 2. alpha = 2/4 = 0.5.
    # idx 3: 40 * 0.5 + 20 * 0.5 = 30.0
    res = ind.compute(data)
    assert res == [None, None, 20.0, 30.0]


def test_ema_bounds() -> None:
    with pytest.raises(ValueError, match="positive"):
        EMA(period=0)


# ------------------------------------------------------------------------------
# RSI Tests
# ------------------------------------------------------------------------------


def test_rsi_spec_and_warmup() -> None:
    ind = RSI(period=3)
    assert ind.spec.name == "rsi"
    assert ind.spec.inputs == ("close",)
    assert ind.spec.outputs == ("value",)
    assert ind.spec.warmup == 3


def test_rsi_constant_prices() -> None:
    ind = RSI(period=2)
    res = ind.compute([10.0, 10.0, 10.0, 10.0])
    assert res[0] is None and res[1] is None
    assert res[2] == 50.0
    assert res[3] == 50.0


def test_rsi_bounds() -> None:
    with pytest.raises(ValueError, match="positive"):
        RSI(period=0)


# ------------------------------------------------------------------------------
# ATR Tests
# ------------------------------------------------------------------------------


def test_atr_spec_and_warmup() -> None:
    ind = ATR(n=5)
    assert ind.spec.name == "atr"
    assert ind.spec.inputs == ("high", "low", "close")
    assert ind.spec.outputs == ("atr",)
    assert ind.spec.warmup == 4


def test_atr_bounds() -> None:
    with pytest.raises(ValueError, match="positive"):
        ATR(n=0)
    with pytest.raises(ValueError, match="positive"):
        ATR(n=-2)


def test_atr_bar_input() -> None:
    ind = ATR(n=2)
    b1 = _make_bar(12.0, 10.0, 11.0)
    b2 = _make_bar(14.0, 11.0, 13.0)
    st = ind.new_state()
    v1 = ind.update(st, b1)
    v2 = ind.update(st, b2)
    assert v1 is None
    # TR1 = 2.0, TR2 = max(14-11, |14-11|, |11-11|) = 3.0. ATR = (2.0+3.0)/2 = 2.5
    assert v2 == 2.5


def test_atr_mapping_input() -> None:
    ind = ATR(n=2)
    data = {
        "high": [12.0, 14.0],
        "low": [10.0, 11.0],
        "close": [11.0, 13.0],
    }
    res = ind.compute(data)
    assert res == [None, 2.5]


# ------------------------------------------------------------------------------
# VWAP Tests
# ------------------------------------------------------------------------------


def test_vwap_spec() -> None:
    ind = VWAP()
    assert ind.spec.name == "vwap"
    assert ind.spec.inputs == ("high", "low", "close", "volume", "new_session")
    assert ind.spec.outputs == ("vwap",)
    assert ind.spec.warmup == 0
    assert ind.spec.timeframes == ("intraday",)


def test_vwap_zero_volume() -> None:
    ind = VWAP()
    data = {"high": [10.0], "low": [10.0], "close": [10.0], "volume": [0.0]}
    assert ind.compute(data) == [None]


def test_vwap_session_reset() -> None:
    ind = VWAP()
    data = {
        "high": [12.0, 14.0, 22.0],
        "low": [10.0, 12.0, 20.0],
        "close": [11.0, 13.0, 21.0],
        "volume": [100.0, 100.0, 100.0],
        "new_session": [True, False, True],
    }
    res = ind.compute(data)
    assert res[0] == 11.0
    assert res[1] == 12.0  # (11*100 + 13*100) / 200 = 12.0
    assert res[2] == 21.0  # Reset to typical price of bar 2 = 21.0


def test_vwap_bar_incremental() -> None:
    ind = VWAP()
    b1 = _make_bar(12.0, 10.0, 11.0, volume=100)
    b2 = _make_bar(14.0, 12.0, 13.0, volume=100)
    st = ind.new_state()
    v1 = ind.update(st, b1)
    v2 = ind.update(st, b2)
    assert v1 == 11.0
    assert v2 == 12.0


# ------------------------------------------------------------------------------
# Bollinger Tests
# ------------------------------------------------------------------------------


def test_bollinger_spec() -> None:
    ind = Bollinger(n=20, k=2.0)
    assert ind.spec.name == "bollinger"
    assert ind.spec.inputs == ("close",)
    assert ind.spec.outputs == ("middle", "upper", "lower")
    assert ind.spec.warmup == 19


def test_bollinger_bounds() -> None:
    with pytest.raises(ValueError, match="positive"):
        Bollinger(n=0, k=2.0)
    with pytest.raises(ValueError, match="positive"):
        Bollinger(n=10, k=0.0)
    with pytest.raises(ValueError, match="positive"):
        Bollinger(n=10, k=-2.0)


def test_bollinger_flat_series() -> None:
    ind = Bollinger(n=3, k=2.0)
    res = ind.compute([10.0, 10.0, 10.0, 10.0])
    assert res["middle"] == [None, None, 10.0, 10.0]
    assert res["upper"] == [None, None, 10.0, 10.0]
    assert res["lower"] == [None, None, 10.0, 10.0]


def test_bollinger_incremental_dict() -> None:
    ind = Bollinger(n=2, k=1.0)
    st = ind.new_state()
    out1 = ind.update(st, 10.0)
    out2 = ind.update(st, 20.0)
    assert out1 == {"middle": None, "upper": None, "lower": None}
    assert out2["middle"] == 15.0
    assert out2["upper"] == 20.0  # std = 5.0
    assert out2["lower"] == 10.0


# ------------------------------------------------------------------------------
# MACD Tests
# ------------------------------------------------------------------------------


def test_macd_spec() -> None:
    ind = MACD(fast=12, slow=26, signal=9)
    assert ind.spec.name == "macd"
    assert ind.spec.inputs == ("close",)
    assert ind.spec.outputs == ("macd", "signal", "histogram")
    assert ind.spec.warmup == 33


def test_macd_bounds() -> None:
    with pytest.raises(ValueError, match="positive"):
        MACD(fast=0, slow=26, signal=9)
    with pytest.raises(ValueError, match="must be less than slow"):
        MACD(fast=26, slow=26, signal=9)
    with pytest.raises(ValueError, match="must be less than slow"):
        MACD(fast=30, slow=26, signal=9)
    with pytest.raises(ValueError, match="positive"):
        MACD(fast=12, slow=26, signal=0)


def test_macd_computation() -> None:
    ind = MACD(fast=2, slow=3, signal=2)
    prices = [10.0, 10.0, 10.0, 10.0, 10.0]
    res = ind.compute(prices)
    # On flat prices, MACD = 0, Signal = 0, Histogram = 0
    assert res["macd"][:2] == [None, None]
    assert res["macd"][2] == 0.0
    assert res["signal"][:3] == [None, None, None]
    assert res["signal"][3] == 0.0
    assert res["histogram"][3] == 0.0


# ------------------------------------------------------------------------------
# RollingHL Tests
# ------------------------------------------------------------------------------


def test_rolling_hl_spec() -> None:
    ind = RollingHL(n=10)
    assert ind.spec.name == "rolling_hl"
    assert ind.spec.inputs == ("high", "low")
    assert ind.spec.outputs == ("high", "low")
    assert ind.spec.warmup == 9


def test_rolling_hl_bounds() -> None:
    with pytest.raises(ValueError, match="positive"):
        RollingHL(n=0)
    with pytest.raises(ValueError, match="positive"):
        RollingHL(n=-3)


def test_rolling_hl_incremental() -> None:
    ind = RollingHL(n=2)
    st = ind.new_state()
    v1 = ind.update(st, {"high": 15.0, "low": 10.0})
    v2 = ind.update(st, {"high": 12.0, "low": 8.0})
    assert v1 == {"high": None, "low": None}
    assert v2 == {"high": 15.0, "low": 8.0}


# ------------------------------------------------------------------------------
# VolumeAvg Tests
# ------------------------------------------------------------------------------


def test_volume_avg_spec() -> None:
    ind = VolumeAvg(n=20)
    assert ind.spec.name == "volume_avg"
    assert ind.spec.inputs == ("volume",)
    assert ind.spec.outputs == ("volume_avg",)
    assert ind.spec.warmup == 19


def test_volume_avg_bounds() -> None:
    with pytest.raises(ValueError, match="positive"):
        VolumeAvg(n=0)


def test_volume_avg_bar_input() -> None:
    ind = VolumeAvg(n=2)
    b1 = _make_bar(10.0, 9.0, 9.5, volume=100)
    b2 = _make_bar(10.0, 9.0, 9.5, volume=300)
    st = ind.new_state()
    assert ind.update(st, b1) is None
    assert ind.update(st, b2) == 200.0


# ------------------------------------------------------------------------------
# ROC Tests
# ------------------------------------------------------------------------------


def test_roc_spec() -> None:
    ind = ROC(n=12)
    assert ind.spec.name == "roc"
    assert ind.spec.inputs == ("close",)
    assert ind.spec.outputs == ("roc",)
    assert ind.spec.warmup == 12


def test_roc_bounds() -> None:
    with pytest.raises(ValueError, match="positive"):
        ROC(n=0)


def test_roc_fraction_output() -> None:
    ind = ROC(n=1)
    res = ind.compute([100.0, 110.0])
    assert res[0] is None
    assert res[1] == pytest.approx(0.10)


# ------------------------------------------------------------------------------
# Hand-computed Vector Validations (at least 2 vectors per indicator)
# ------------------------------------------------------------------------------


def test_hand_vector_sma_1() -> None:
    # 5 values, n=3
    res = SMA(period=3).compute([10.0, 20.0, 30.0, 40.0, 50.0])
    assert res == [None, None, 20.0, 30.0, 40.0]


def test_hand_vector_sma_2() -> None:
    # 4 values, n=2
    res = SMA(period=2).compute([1.0, 3.0, 5.0, 7.0])
    assert res == [None, 2.0, 4.0, 6.0]


def test_hand_vector_ema_1() -> None:
    # 4 values, n=2: seed at 1 = (10+20)/2 = 15.0. alpha = 2/3.
    # idx 2: 30 * 2/3 + 15 * 1/3 = 25.0
    # idx 3: 40 * 2/3 + 25 * 1/3 = 35.0
    res = EMA(period=2).compute([10.0, 20.0, 30.0, 40.0])
    assert res[0] is None
    assert res[1] == pytest.approx(15.0)
    assert res[2] == pytest.approx(25.0)
    assert res[3] == pytest.approx(35.0)


def test_hand_vector_ema_2() -> None:
    # flat prices
    res = EMA(period=3).compute([5.0, 5.0, 5.0, 5.0])
    assert res == [None, None, 5.0, 5.0]


def test_hand_vector_rsi_1() -> None:
    # All positive changes -> RSI = 100.0
    res = RSI(period=2).compute([10.0, 20.0, 30.0, 40.0])
    assert res[0] is None and res[1] is None
    assert res[2] == 100.0
    assert res[3] == 100.0


def test_hand_vector_rsi_2() -> None:
    # All negative changes -> RSI = 0.0
    res = RSI(period=2).compute([40.0, 30.0, 20.0, 10.0])
    assert res[0] is None and res[1] is None
    assert res[2] == 0.0
    assert res[3] == 0.0


def test_hand_vector_atr_1() -> None:
    # Flat range: H=15, L=10 -> TR=5
    data = {"high": [15.0, 15.0, 15.0], "low": [10.0, 10.0, 10.0], "close": [12.0, 12.0, 12.0]}
    res = ATR(n=2).compute(data)
    assert res == [None, 5.0, 5.0]


def test_hand_vector_atr_2() -> None:
    # Gap up: prev close = 10, next bar H=20, L=18 -> TR = max(2, 10, 8) = 10
    data = {"high": [12.0, 20.0], "low": [10.0, 18.0], "close": [10.0, 19.0]}
    res = ATR(n=2).compute(data)
    # TR0 = 2, TR1 = 10 -> mean = 6.0
    assert res == [None, 6.0]


def test_hand_vector_vwap_1() -> None:
    data = {
        "high": [12.0, 12.0],
        "low": [10.0, 10.0],
        "close": [11.0, 11.0],
        "volume": [10.0, 10.0],
    }
    res = VWAP().compute(data)
    assert res == [11.0, 11.0]


def test_hand_vector_vwap_2() -> None:
    data = {
        "high": [10.0, 20.0],
        "low": [10.0, 20.0],
        "close": [10.0, 20.0],
        "volume": [100.0, 300.0],
    }
    # Bar 0: tp=10, v=100 -> 1000/100 = 10.0
    # Bar 1: tp=20, v=300 -> pv=6000. cum_pv=7000, cum_v=400 -> 7000/400 = 17.5
    res = VWAP().compute(data)
    assert res == [10.0, 17.5]


def test_hand_vector_bollinger_1() -> None:
    res = Bollinger(n=2, k=1.0).compute([10.0, 20.0])
    # win = [10, 20], mean = 15. var = (25+25)/2 = 25 -> std = 5
    assert res["middle"] == [None, 15.0]
    assert res["upper"] == [None, 20.0]
    assert res["lower"] == [None, 10.0]


def test_hand_vector_bollinger_2() -> None:
    res = Bollinger(n=2, k=2.0).compute([10.0, 30.0])
    # mean = 20, var = (100+100)/2 = 100 -> std = 10
    assert res["middle"] == [None, 20.0]
    assert res["upper"] == [None, 40.0]
    assert res["lower"] == [None, 0.0]


def test_hand_vector_macd_1() -> None:
    # fast=2, slow=3, signal=2
    res = MACD(fast=2, slow=3, signal=2).compute([10.0, 10.0, 10.0, 10.0])
    assert res["macd"] == [None, None, 0.0, 0.0]
    assert res["signal"] == [None, None, None, 0.0]
    assert res["histogram"] == [None, None, None, 0.0]


def test_hand_vector_macd_2() -> None:
    # Step change
    res = MACD(fast=2, slow=3, signal=2).compute([10.0, 10.0, 10.0, 20.0])
    # At idx 2: flat -> macd=0.0
    # At idx 3: fast EMA = 20*2/3 + 10*1/3 = 16.6667. slow EMA = 20*2/4 + 10*2/4 = 15.0.
    # macd = 1.6667
    assert res["macd"][2] == 0.0
    assert res["macd"][3] is not None and res["macd"][3] > 0.0


def test_hand_vector_rolling_hl_1() -> None:
    data = {"high": [10.0, 20.0, 15.0], "low": [5.0, 8.0, 3.0]}
    res = RollingHL(n=2).compute(data)
    assert res["high"] == [None, 20.0, 20.0]
    assert res["low"] == [None, 5.0, 3.0]


def test_hand_vector_rolling_hl_2() -> None:
    data = {"high": [100.0, 200.0], "low": [50.0, 60.0]}
    res = RollingHL(n=1).compute(data)
    assert res["high"] == [100.0, 200.0]
    assert res["low"] == [50.0, 60.0]


def test_hand_vector_volume_avg_1() -> None:
    res = VolumeAvg(n=2).compute([10.0, 20.0, 30.0])
    assert res == [None, 15.0, 25.0]


def test_hand_vector_volume_avg_2() -> None:
    res = VolumeAvg(n=3).compute([100.0, 100.0, 100.0])
    assert res == [None, None, 100.0]


def test_hand_vector_roc_1() -> None:
    res = ROC(n=1).compute([100.0, 150.0])
    assert res[0] is None
    assert res[1] == pytest.approx(0.50)


def test_hand_vector_roc_2() -> None:
    res = ROC(n=2).compute([100.0, 200.0, 50.0])
    # 50 / 100 - 1 = -0.50
    assert res[0] is None and res[1] is None
    assert res[2] == pytest.approx(-0.50)


# ------------------------------------------------------------------------------
# Look-ahead Bias Prevention Tests
# ------------------------------------------------------------------------------


def test_lookahead_atr() -> None:
    ind = ATR(n=2)
    h = [12.0, 14.0, 15.0, 18.0]
    lo = [10.0, 11.0, 12.0, 14.0]
    c = [11.0, 13.0, 14.0, 17.0]
    full = ind.compute({"high": h, "low": lo, "close": c})
    prefix = ind.compute({"high": h[:3], "low": lo[:3], "close": c[:3]})
    assert prefix == full[:3]


def test_lookahead_vwap() -> None:
    ind = VWAP()
    h = [12.0, 14.0, 15.0, 18.0]
    lo = [10.0, 11.0, 12.0, 14.0]
    c = [11.0, 13.0, 14.0, 17.0]
    v = [100.0, 200.0, 300.0, 400.0]
    full = ind.compute({"high": h, "low": lo, "close": c, "volume": v})
    prefix = ind.compute({"high": h[:3], "low": lo[:3], "close": c[:3], "volume": v[:3]})
    assert prefix == full[:3]


def test_lookahead_rolling_hl() -> None:
    ind = RollingHL(n=2)
    h = [12.0, 14.0, 15.0, 18.0]
    lo = [10.0, 11.0, 12.0, 14.0]
    full = ind.compute({"high": h, "low": lo})
    prefix = ind.compute({"high": h[:3], "low": lo[:3]})
    assert prefix["high"] == full["high"][:3]
    assert prefix["low"] == full["low"][:3]


def test_lookahead_volume_avg() -> None:
    ind = VolumeAvg(n=2)
    v = [100.0, 200.0, 300.0, 400.0]
    full = ind.compute(v)
    prefix = ind.compute(v[:3])
    assert prefix == full[:3]
