"""Unit tests and golden vector validations for builtin indicators."""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from algodesk.api.manifest import PluginManifest

from ..indicators import EMA, RSI, SMA
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


# ------------------------------------------------------------------------------
# Golden Vector Tests
# ------------------------------------------------------------------------------


def test_sma_golden_vectors() -> None:
    data = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0]
    sma = SMA(period=3)
    computed = sma.compute(data)
    expected = _ref_sma(data, 3)

    assert computed[0] is None
    assert computed[1] is None
    assert computed[2] == 11.0  # (10 + 11 + 12) / 3
    assert computed[3] == 12.0  # (11 + 12 + 13) / 3
    assert computed == expected


def test_ema_golden_vectors() -> None:
    data = [2.0, 4.0, 6.0, 8.0, 10.0]
    ema = EMA(period=3)
    computed = ema.compute(data)
    expected = _ref_ema(data, 3)

    # alpha = 2 / (3 + 1) = 0.5
    # Seed at index 2 = (2 + 4 + 6) / 3 = 4.0
    # Next at index 3 = 0.5 * 8.0 + 0.5 * 4.0 = 6.0
    # Next at index 4 = 0.5 * 10.0 + 0.5 * 6.0 = 8.0
    assert computed[0] is None
    assert computed[1] is None
    assert computed[2] == pytest.approx(4.0)
    assert computed[3] == pytest.approx(6.0)
    assert computed[4] == pytest.approx(8.0)
    for c, e in zip(computed, expected, strict=True):
        if e is None:
            assert c is None
        else:
            assert c == pytest.approx(e)


def test_rsi_golden_vectors() -> None:
    # 5 prices with period 3:
    # Changes: +1, +2, +3 (all gains, no loss)
    data = [10.0, 11.0, 13.0, 16.0, 15.0]
    rsi = RSI(period=3)
    computed = rsi.compute(data)
    expected = _ref_rsi(data, 3)

    assert computed[0] is None
    assert computed[1] is None
    assert computed[2] is None
    # Index 3: 3 changes: (+1, +2, +3). avg_gain=2.0, avg_loss=0.0 -> 100.0
    assert computed[3] == 100.0
    # Index 4: change is -1.0 (loss=1.0). avg_gain = (2*2 + 0)/3 = 4/3. avg_loss = (0*2 + 1)/3 = 1/3
    # RS = 4. RSI = 100 - (100 / 5) = 80.0
    assert computed[4] == pytest.approx(80.0)

    for c, e in zip(computed, expected, strict=True):
        if e is None:
            assert c is None
        else:
            assert c == pytest.approx(e)


def test_rsi_boundary_cases() -> None:
    rsi = RSI(period=2)

    # All flat: delta = 0, both gain and loss are 0 -> 50.0
    flat_data = [100.0, 100.0, 100.0]
    flat_res = rsi.compute(flat_data)
    assert flat_res[2] == 50.0

    # All falling: loss > 0, gain = 0 -> 0.0
    fall_data = [100.0, 90.0, 80.0]
    fall_res = rsi.compute(fall_data)
    assert fall_res[2] == 0.0


# ------------------------------------------------------------------------------
# Hypothesis Property Tests: Batch Equals Incremental
# ------------------------------------------------------------------------------


@given(
    st.lists(
        st.floats(min_value=-10000.0, max_value=10000.0, allow_nan=False),
        min_size=1,
        max_size=50,
    )
)
def test_sma_hypothesis_batch_equals_incremental(data: list[float]) -> None:
    sma = SMA(period=5)
    batch = sma.compute(data)
    state = sma.new_state()
    inc = [sma.update(state, v) for v in data]

    assert len(batch) == len(inc)
    for b, i in zip(batch, inc, strict=True):
        if b is None:
            assert i is None
        else:
            assert i is not None
            assert math.isclose(b, i, rel_tol=1e-7, abs_tol=1e-7)


@given(
    st.lists(
        st.floats(min_value=-10000.0, max_value=10000.0, allow_nan=False),
        min_size=1,
        max_size=50,
    )
)
def test_ema_hypothesis_batch_equals_incremental(data: list[float]) -> None:
    ema = EMA(period=4)
    batch = ema.compute(data)
    state = ema.new_state()
    inc = [ema.update(state, v) for v in data]

    assert len(batch) == len(inc)
    for b, i in zip(batch, inc, strict=True):
        if b is None:
            assert i is None
        else:
            assert i is not None
            assert math.isclose(b, i, rel_tol=1e-7, abs_tol=1e-7)


@given(
    st.lists(
        st.floats(min_value=1.0, max_value=10000.0, allow_nan=False),
        min_size=1,
        max_size=50,
    )
)
def test_rsi_hypothesis_batch_equals_incremental(data: list[float]) -> None:
    rsi = RSI(period=4)
    batch = rsi.compute(data)
    state = rsi.new_state()
    inc = [rsi.update(state, v) for v in data]

    assert len(batch) == len(inc)
    for b, i in zip(batch, inc, strict=True):
        if b is None:
            assert i is None
        else:
            assert i is not None
            assert math.isclose(b, i, rel_tol=1e-6, abs_tol=1e-6)


# ------------------------------------------------------------------------------
# Plugin Conformance and Gating Tests
# ------------------------------------------------------------------------------


def test_builtin_plugin_conformance() -> None:
    manifest = PluginManifest(
        name="builtin",
        kind="indicator",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="plugin:BuiltinIndicatorsPlugin",
        capabilities={"sma": True, "ema": True, "rsi": True},
    )
    plugin = BuiltinIndicatorsPlugin(manifest, {"indicator": "sma", "period": 5})

    assert plugin.supports("sma") is True
    assert plugin.supports("ema") is True
    assert plugin.supports("rsi") is True

    # Test factory method
    sma = plugin.get_indicator("sma", period=10)
    assert sma.spec.name == "sma"
    ema = plugin.get_indicator("ema", period=10)
    assert ema.spec.name == "ema"
    rsi = plugin.get_indicator("rsi", period=14)
    assert rsi.spec.name == "rsi"

    with pytest.raises(ValueError, match="Unknown builtin indicator"):
        plugin.get_indicator("macd")
