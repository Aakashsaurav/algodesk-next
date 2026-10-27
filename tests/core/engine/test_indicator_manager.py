"""Unit tests for IndicatorManager covering warm-up, incomplete bars, and caching."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from plugins.indicators.builtin.plugin import BuiltinIndicatorsPlugin

from algodesk.api.market import Bar, InstrumentId
from algodesk.engine.indicator_manager import IndicatorManager


@pytest.fixture
def ind_plugin() -> BuiltinIndicatorsPlugin:
    manifest = MagicMock()
    manifest.capabilities = {"sma": True, "ema": True, "rsi": True}
    return BuiltinIndicatorsPlugin(manifest, config={})


def test_indicator_manager_requires_completed_bars(ind_plugin: BuiltinIndicatorsPlugin) -> None:
    journal = MagicMock()
    mgr = IndicatorManager(indicator_plugin=ind_plugin, journal=journal)
    inst = InstrumentId("NSE:EQ:RELIANCE")

    mgr.register("sma", inst, "5m", period=3)
    now_dt = datetime(2026, 10, 26, 9, 30, tzinfo=UTC)

    # Incomplete bar must be ignored
    incomplete_bar = Bar(
        instrument=inst,
        timeframe="5m",
        start_ts=now_dt,
        end_ts=now_dt + timedelta(minutes=5),
        open=100.0,
        high=105.0,
        low=99.0,
        close=102.0,
        volume=10,
        complete=False,
        synthetic=False,
    )
    mgr.on_bar(incomplete_bar)
    assert mgr.get("sma", inst, "5m", period=3) is None

    # Feed completed bars
    for i, p in enumerate([100.0, 102.0, 104.0]):
        b = Bar(
            instrument=inst,
            timeframe="5m",
            start_ts=now_dt + timedelta(minutes=5 * i),
            end_ts=now_dt + timedelta(minutes=5 * (i + 1)),
            open=p,
            high=p + 1,
            low=p - 1,
            close=p,
            volume=100,
            complete=True,
            synthetic=False,
        )
        mgr.on_bar(b)

    val = mgr.get("sma", inst, "5m", period=3)
    assert val is not None
    assert val == pytest.approx(102.0)


def test_indicator_manager_warmup_incomplete(ind_plugin: BuiltinIndicatorsPlugin) -> None:
    journal = MagicMock()
    mgr = IndicatorManager(indicator_plugin=ind_plugin, journal=journal)
    inst = InstrumentId("NSE:EQ:TCS")

    mgr.register("sma", inst, "5m", period=10)

    # Mock DataService providing fewer bars than warmup
    session_start = datetime(2026, 10, 26, 9, 15, tzinfo=UTC)
    mock_ds = MagicMock()
    # Provide only 3 bars when 10 are needed
    mock_bars = [
        Bar(
            instrument=inst,
            timeframe="5m",
            start_ts=session_start - timedelta(minutes=15),
            end_ts=session_start - timedelta(minutes=10),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=50,
            complete=True,
            synthetic=False,
        )
    ]
    mock_ds.history.return_value = mock_bars

    mgr.warmup(mock_ds, session_start=session_start)

    # Must not be ready and must journal warmup_incomplete
    assert not mgr.is_ready("sma", inst, "5m", period=10)
    assert mgr.get("sma", inst, "5m", period=10) is None
    journal.append.assert_called_with(
        "strategy.warmup_incomplete",
        {
            "instrument": str(inst),
            "timeframe": "5m",
            "indicator": "sma",
            "warmup_needed": 10,
            "bars_available": 1,
        },
    )
