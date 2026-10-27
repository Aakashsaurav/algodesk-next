"""Unit tests for StrategyContext and BarBuffer immutability and accessor behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from algodesk.api import Instrument, InstrumentId, Product
from algodesk.api.market import Bar
from algodesk.api.trading import Position
from algodesk.engine.context import BarBuffer, build_strategy_context
from algodesk.engine.indicator_manager import IndicatorManager


def test_bar_buffer_retrieval_and_filtering() -> None:
    buf = BarBuffer(max_len=5)
    inst = InstrumentId("NSE:EQ:RELIANCE")
    t0 = datetime(2026, 10, 26, 9, 15, tzinfo=UTC)

    # Incomplete bar discarded
    inc = Bar(
        instrument=inst,
        timeframe="5m",
        start_ts=t0,
        end_ts=t0 + timedelta(minutes=5),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=100,
        complete=False,
        synthetic=False,
    )
    buf.append(inc)
    assert buf.get(inst, "5m", 1) == ()

    # Append 3 completed bars
    for i in range(3):
        b = Bar(
            instrument=inst,
            timeframe="5m",
            start_ts=t0 + timedelta(minutes=5 * i),
            end_ts=t0 + timedelta(minutes=5 * (i + 1)),
            open=100.0 + i,
            high=102.0 + i,
            low=98.0 + i,
            close=101.0 + i,
            volume=500,
            complete=True,
            synthetic=False,
        )
        buf.append(b)

    bars_2 = buf.get(inst, "5m", 2)
    assert len(bars_2) == 2
    assert bars_2[0].close == 102.0
    assert bars_2[1].close == 103.0


def test_strategy_context_read_only() -> None:
    inst = InstrumentId("NSE:EQ:INFY")
    instrument_obj = Instrument(
        id=inst,
        exchange="NSE",
        segment="EQ",
        symbol="INFY",
        kind="equity",
        lot_size=1,
        tick_size=Decimal("0.05"),
        expiry=None,
        underlying=None,
        isin=None,
        broker_keys={},
    )
    clock = MagicMock()
    buf = BarBuffer()
    ind_mgr = MagicMock(spec=IndicatorManager)
    ind_mgr.get.return_value = 142.5

    pos = Position(
        instrument=inst,
        product=Product.CNC,
        qty=10,
        avg_price=Decimal("1500.0"),
        realized=Decimal("0.0"),
        unrealized=Decimal("50.0"),
    )

    ctx = build_strategy_context(
        clock=clock,
        instruments={inst: instrument_obj},
        indicator_manager=ind_mgr,
        bar_buffer=buf,
        position_fn=lambda i: pos if i == inst else None,
        portfolio_fn=lambda: "portfolio_snapshot",
        params={"fast": 10, "slow": 20},
        risk_state={"killswitch": False},
    )

    # Strategy cannot mutate read-only mappings
    with pytest.raises((TypeError, AttributeError)):
        ctx.instruments[inst] = instrument_obj  # type: ignore[index]

    with pytest.raises((TypeError, AttributeError)):
        ctx.params["fast"] = 15  # type: ignore[index]

    with pytest.raises((TypeError, AttributeError)):
        ctx.risk_state["killswitch"] = True  # type: ignore[index]

    # Verify accessor methods
    assert ctx.indicator("sma", inst, "5m", period=10) == 142.5
    ind_mgr.get.assert_called_with("sma", inst, "5m", period=10)

    p = ctx.position(inst)
    assert p is not None
    assert p.qty == 10
    assert ctx.portfolio == "portfolio_snapshot"
