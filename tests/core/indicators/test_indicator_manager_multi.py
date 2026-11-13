"""Tests for IndicatorManager multi-input, multi-output, and timeframe gating."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from plugins.indicators.builtin.plugin import BuiltinIndicatorsPlugin

from algodesk.api.manifest import PluginManifest
from algodesk.api.market import Bar, InstrumentId
from algodesk.engine.context import BarBuffer, build_strategy_context
from algodesk.engine.indicator_manager import IndicatorManager


def _make_bar(
    inst: InstrumentId,
    tf: str,
    high: float,
    low: float,
    close: float,
    volume: int = 100,
    dt: datetime | None = None,
    complete: bool = True,
) -> Bar:
    t = dt or datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
    return Bar(
        instrument=inst,
        timeframe=tf,
        start_ts=t,
        end_ts=t + timedelta(minutes=5),
        open=close,
        high=high,
        low=low,
        close=close,
        volume=volume,
        complete=complete,
        synthetic=False,
    )


@pytest.fixture
def ind_plugin() -> BuiltinIndicatorsPlugin:
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
    return BuiltinIndicatorsPlugin(manifest, config={})


def test_indicator_manager_multi_output_bollinger(
    ind_plugin: BuiltinIndicatorsPlugin,
) -> None:
    mgr = IndicatorManager(indicator_plugin=ind_plugin)
    inst = InstrumentId("NSE:EQ:RELIANCE")
    mgr.register("bollinger", inst, "5m", n=3, k=2.0)

    # Feed 3 bars
    base_t = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
    for i, c in enumerate([10.0, 20.0, 30.0]):
        b = _make_bar(inst, "5m", c, c, c, dt=base_t + timedelta(minutes=5 * i))
        mgr.on_bar(b)

    # Output defaults to first output ("middle")
    assert mgr.get("bollinger", inst, "5m", n=3, k=2.0) == 20.0
    assert mgr.get("bollinger", inst, "5m", output="middle", n=3, k=2.0) == 20.0
    # Population std dev = sqrt(200/3) = 8.1649658
    upper = mgr.get("bollinger", inst, "5m", output="upper", n=3, k=2.0)
    lower = mgr.get("bollinger", inst, "5m", output="lower", n=3, k=2.0)
    assert upper is not None and upper > 20.0
    assert lower is not None and lower < 20.0


def test_indicator_manager_multi_output_macd(
    ind_plugin: BuiltinIndicatorsPlugin,
) -> None:
    mgr = IndicatorManager(indicator_plugin=ind_plugin)
    inst = InstrumentId("NSE:EQ:TCS")
    mgr.register("macd", inst, "1d", fast=2, slow=4, signal=2)

    base_t = datetime(2026, 11, 1, 9, 15, tzinfo=UTC)
    prices = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
    for i, p in enumerate(prices):
        b = _make_bar(inst, "1d", p, p, p, dt=base_t + timedelta(days=i))
        mgr.on_bar(b)

    # All 3 outputs should be available after warmup
    macd_val = mgr.get("macd", inst, "1d", output="macd", fast=2, slow=4, signal=2)
    sig_val = mgr.get("macd", inst, "1d", output="signal", fast=2, slow=4, signal=2)
    hist_val = mgr.get("macd", inst, "1d", output="histogram", fast=2, slow=4, signal=2)
    default_val = mgr.get("macd", inst, "1d", fast=2, slow=4, signal=2)

    assert macd_val is not None
    assert sig_val is not None
    assert hist_val is not None
    assert default_val == macd_val
    assert hist_val == pytest.approx(macd_val - sig_val)


def test_indicator_manager_multi_input_atr(
    ind_plugin: BuiltinIndicatorsPlugin,
) -> None:
    mgr = IndicatorManager(indicator_plugin=ind_plugin)
    inst = InstrumentId("NSE:EQ:INFY")
    mgr.register("atr", inst, "5m", n=2)

    base_t = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
    b1 = _make_bar(inst, "5m", 12.0, 10.0, 11.0, dt=base_t)
    b2 = _make_bar(inst, "5m", 14.0, 11.0, 13.0, dt=base_t + timedelta(minutes=5))
    mgr.on_bar(b1)
    assert mgr.get("atr", inst, "5m", n=2) is None
    mgr.on_bar(b2)
    # TR1 = 2.0, TR2 = max(3, 3, 0) = 3.0 -> ATR = 2.5
    assert mgr.get("atr", inst, "5m", n=2) == 2.5


def test_indicator_manager_volume_avg(
    ind_plugin: BuiltinIndicatorsPlugin,
) -> None:
    mgr = IndicatorManager(indicator_plugin=ind_plugin)
    inst = InstrumentId("NSE:EQ:INFY")
    mgr.register("volume_avg", inst, "5m", n=2)

    base_t = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
    b1 = _make_bar(inst, "5m", 10.0, 9.0, 9.5, volume=100, dt=base_t)
    b2 = _make_bar(inst, "5m", 10.0, 9.0, 9.5, volume=300, dt=base_t + timedelta(minutes=5))
    mgr.on_bar(b1)
    assert mgr.get("volume_avg", inst, "5m", n=2) is None
    mgr.on_bar(b2)
    assert mgr.get("volume_avg", inst, "5m", n=2) == 200.0


def test_indicator_manager_vwap_timeframe_gate(
    ind_plugin: BuiltinIndicatorsPlugin,
) -> None:
    mgr = IndicatorManager(indicator_plugin=ind_plugin)
    inst = InstrumentId("NSE:EQ:SBIN")
    with pytest.raises(ValueError, match="intraday"):
        mgr.register("vwap", inst, "1d")

    # Intraday succeeds
    mgr.register("vwap", inst, "5m")
    mgr.register("vwap", inst, "1m")


def test_indicator_manager_context_wiring(
    ind_plugin: BuiltinIndicatorsPlugin,
) -> None:
    mgr = IndicatorManager(indicator_plugin=ind_plugin)
    inst = InstrumentId("NSE:EQ:RELIANCE")
    mgr.register("bollinger", inst, "5m", n=2, k=2.0)

    base_t = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
    mgr.on_bar(_make_bar(inst, "5m", 10.0, 10.0, 10.0, dt=base_t))
    mgr.on_bar(_make_bar(inst, "5m", 20.0, 20.0, 20.0, dt=base_t + timedelta(minutes=5)))

    # Use build_strategy_context facade
    clock = MagicMock()
    buf = BarBuffer()
    ctx = build_strategy_context(
        clock=clock,
        instruments={},
        bar_buffer=buf,
        indicator_manager=mgr,
        position_fn=lambda _: None,
        portfolio_fn=lambda: MagicMock(),
    )
    val_mid = ctx.indicator("bollinger", inst, "5m", output="middle", n=2, k=2.0)
    val_def = ctx.indicator("bollinger", inst, "5m", n=2, k=2.0)
    assert val_mid == 15.0
    assert val_def == 15.0


def test_indicator_manager_incomplete_bars_ignored(
    ind_plugin: BuiltinIndicatorsPlugin,
) -> None:
    mgr = IndicatorManager(indicator_plugin=ind_plugin)
    inst = InstrumentId("NSE:EQ:RELIANCE")
    mgr.register("sma", inst, "5m", period=2)

    base_t = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
    b_inc = _make_bar(inst, "5m", 10.0, 10.0, 10.0, dt=base_t, complete=False)
    mgr.on_bar(b_inc)
    assert mgr.get("sma", inst, "5m", period=2) is None


def test_indicator_manager_independent_instruments(
    ind_plugin: BuiltinIndicatorsPlugin,
) -> None:
    mgr = IndicatorManager(indicator_plugin=ind_plugin)
    i1 = InstrumentId("NSE:EQ:A")
    i2 = InstrumentId("NSE:EQ:B")
    mgr.register("sma", i1, "1d", period=2)
    mgr.register("sma", i2, "1d", period=2)

    base_t = datetime(2026, 11, 1, 9, 15, tzinfo=UTC)
    mgr.on_bar(_make_bar(i1, "1d", 10.0, 10.0, 10.0, dt=base_t))
    mgr.on_bar(_make_bar(i1, "1d", 20.0, 20.0, 20.0, dt=base_t + timedelta(days=1)))
    mgr.on_bar(_make_bar(i2, "1d", 100.0, 100.0, 100.0, dt=base_t))
    mgr.on_bar(_make_bar(i2, "1d", 200.0, 200.0, 200.0, dt=base_t + timedelta(days=1)))

    assert mgr.get("sma", i1, "1d", period=2) == 15.0
    assert mgr.get("sma", i2, "1d", period=2) == 150.0
