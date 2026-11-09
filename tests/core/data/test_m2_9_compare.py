"""Tests for data compare command and bar reconstruction."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from typer.testing import CliRunner

from algodesk.api.errors import DataError
from algodesk.api.market import InstrumentId, Tick
from algodesk.api.protocols import Bar
from algodesk.cli.app import app
from algodesk.data.compare import (
    _build_bars_from_ticks,
    _compare_bar_series,
    compare_ticks_with_source,
)
from algodesk.engine.bars import BarBuilder


def _make_ticks(inst: InstrumentId, start_dt: datetime, count: int = 120) -> list[Tick]:
    """Generate sequential ticks across several minutes."""
    ticks: list[Tick] = []
    base_price = Decimal("2500.00")
    for i in range(count):
        t_ts = start_dt + timedelta(seconds=i * 5)
        price = base_price + Decimal(str(i % 5))
        ticks.append(
            Tick(
                instrument=inst,
                exch_ts=t_ts,
                recv_ts=t_ts,
                ltp=float(price),
                ltq=10,
                volume=10 * (i + 1),
            )
        )
    return ticks


def test_build_bars_from_ticks_in_memory() -> None:
    inst = InstrumentId("NSE:EQ:RELIANCE")
    start = datetime(2026, 11, 5, 9, 15, tzinfo=UTC)
    ticks = _make_ticks(inst, start, count=120)  # 10 minutes of ticks

    def _factory(on_close: Any) -> Any:
        return BarBuilder(timeframes=("1m",), fill_empty_bars=False, on_bar_closed=on_close)

    bars = _build_bars_from_ticks(ticks, builder_factory=_factory)
    assert len(bars) >= 9
    for b in bars:
        assert b.timeframe == "1m"
        assert b.open > 0
        assert b.close > 0
        assert b.high >= b.low
        assert b.volume >= 0


def test_compare_bar_series_identical_bars() -> None:
    inst = InstrumentId("NSE:EQ:RELIANCE")
    dt1 = datetime(2026, 11, 5, 9, 15, tzinfo=UTC)
    dt2 = datetime(2026, 11, 5, 9, 16, tzinfo=UTC)

    b1 = Bar(
        instrument=inst,
        timeframe="1m",
        start_ts=dt1,
        end_ts=dt1 + timedelta(minutes=1),
        open=100.0,
        high=105.0,
        low=99.0,
        close=102.0,
        volume=100,
    )
    b2 = Bar(
        instrument=inst,
        timeframe="1m",
        start_ts=dt2,
        end_ts=dt2 + timedelta(minutes=1),
        open=102.0,
        high=104.0,
        low=101.0,
        close=103.0,
        volume=150,
    )

    res = _compare_bar_series([b1, b2], [b1, b2], tolerance_pct=0.1, symbol="RELIANCE")
    assert res.matched_count == 2
    assert res.max_close_diff == 0.0
    assert res.median_close_diff == 0.0
    assert res.count_above_tolerance == 0
    assert res.max_volume_diff == 0


def test_compare_bar_series_detects_deviations() -> None:
    inst = InstrumentId("NSE:EQ:RELIANCE")
    dt = datetime(2026, 11, 5, 9, 15, tzinfo=UTC)
    b_built = Bar(
        instrument=inst,
        timeframe="1m",
        start_ts=dt,
        end_ts=dt + timedelta(minutes=1),
        open=100.0,
        high=105.0,
        low=99.0,
        close=102.0,
        volume=100,
    )
    # Source has different close (105.0) and volume (200)
    b_src = Bar(
        instrument=inst,
        timeframe="1m",
        start_ts=dt,
        end_ts=dt + timedelta(minutes=1),
        open=100.0,
        high=105.0,
        low=99.0,
        close=105.0,
        volume=200,
    )

    res = _compare_bar_series([b_built], [b_src], tolerance_pct=0.1, symbol="RELIANCE")
    assert res.matched_count == 1
    assert res.max_close_diff == 3.0
    assert res.count_above_tolerance == 1
    assert res.max_volume_diff == 100


def test_compare_ticks_with_source_raises_on_empty_date(tmp_project: Any) -> None:
    with pytest.raises(DataError) as exc:
        compare_ticks_with_source(
            date_val=date(2022, 1, 1),
            symbols=None,
            source="yfinance",
            runtime=tmp_project,
        )
    assert "No recorded ticks found" in exc.value.what


def test_cli_data_compare_invalid_date_format() -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["data", "compare", "--date", "invalid-date", "--symbols", "RELIANCE"])
    assert res.exit_code != 0
    assert "Invalid date format" in res.output or "invalid" in res.output.lower()
