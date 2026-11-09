"""Tests for data smoke command and verification logic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

from typer.testing import CliRunner

from algodesk.api.market import Bar, InstrumentId
from algodesk.cli.app import app
from algodesk.data.smoke import (
    _measure_secondary_diff,
    _resolve_smoke_dates,
    run_data_smoke,
)


def _sample_bars(inst_id: InstrumentId, count: int, base_price: float = 2500.0) -> list[Bar]:
    bars = []
    base_dt = datetime(2026, 11, 4, 9, 15, tzinfo=UTC)
    for i in range(count):
        ts = base_dt + timedelta(minutes=i)
        bars.append(
            Bar(
                instrument=inst_id,
                timeframe="1m",
                start_ts=ts,
                end_ts=ts + timedelta(minutes=1),
                open=base_price,
                high=base_price + 50.0,
                low=base_price - 50.0,
                close=base_price + float(i),
                volume=1000,
            )
        )
    return bars


def test_resolve_smoke_dates() -> None:
    cal = MagicMock()
    cal.is_trading_day.side_effect = lambda d: d.weekday() < 5
    now = datetime(2026, 11, 6, 16, 0, tzinfo=UTC)
    s_1d, s_1m, e_1d, e_1m = _resolve_smoke_dates(cal, now)
    assert s_1d <= s_1m
    assert e_1d == e_1m


def test_measure_secondary_diff() -> None:
    inst_id = InstrumentId("NSE:EQ:RELIANCE")
    pri_bars = _sample_bars(inst_id, 3, base_price=2500.0)
    sec_bars = _sample_bars(inst_id, 3, base_price=2501.0)

    s_pri = MagicMock()
    s_pri.bars = pri_bars

    ds_sec = MagicMock()
    s_sec = MagicMock()
    s_sec.bars = sec_bars
    ds_sec.get.return_value = s_sec

    diff = _measure_secondary_diff(
        ds_sec,
        "NSE:EQ:RELIANCE",
        pri_bars[0].start_ts,
        pri_bars[-1].end_ts,
        "yfinance",
        s_pri,
    )
    assert abs(diff - 1.0) < 1e-5


def test_run_data_smoke_success(tmp_project: Any) -> None:
    inst_id = InstrumentId("NSE:EQ:RELIANCE")
    bars_1d = _sample_bars(inst_id, 5, base_price=2500.0)
    bars_1m = _sample_bars(inst_id, 10, base_price=2500.0)

    slice_1d = MagicMock()
    slice_1d.bars = bars_1d
    slice_1d.gaps = []

    slice_1m = MagicMock()
    slice_1m.bars = bars_1m
    slice_1m.gaps = []

    mock_source = MagicMock()
    mock_source.history = MagicMock(return_value=[])

    ds_pri = MagicMock()
    ds_pri.get.side_effect = [slice_1d, slice_1m, slice_1d, slice_1m]
    ds_pri._resolve_source.return_value = mock_source

    ds_sec = MagicMock()
    sec_slice = MagicMock()
    sec_slice.bars = bars_1d
    ds_sec.get.return_value = sec_slice

    report = run_data_smoke(
        symbols=["NSE:EQ:RELIANCE"],
        runtime=tmp_project,
        primary_source="upstox",
        secondary_source="yfinance",
        data_service_primary=ds_pri,
        data_service_secondary=ds_sec,
    )

    assert report.overall_passed is True
    assert len(report.results) == 1
    res = report.results[0]
    assert res.network_requests_run2 == 0
    assert res.gaps_count == 0
    assert res.verify_passed is True
    assert res.max_close_diff_vs_source2 == 0.0


def test_run_data_smoke_network_failure(tmp_project: Any) -> None:
    inst_id = InstrumentId("NSE:EQ:RELIANCE")
    bars = _sample_bars(inst_id, 2)

    slice_mock = MagicMock()
    slice_mock.bars = bars
    slice_mock.gaps = []

    mock_source = MagicMock()

    ds_pri = MagicMock()

    # When get is called, simulate that run 2 calls plugin history
    def _side_effect_get(*args: Any, **kwargs: Any) -> Any:
        mock_source.history()
        return slice_mock

    ds_pri.get.side_effect = _side_effect_get
    ds_pri._resolve_source.return_value = mock_source

    ds_sec = MagicMock()
    ds_sec.get.return_value = slice_mock

    report = run_data_smoke(
        symbols=["NSE:EQ:RELIANCE"],
        runtime=tmp_project,
        primary_source="upstox",
        secondary_source="yfinance",
        data_service_primary=ds_pri,
        data_service_secondary=ds_sec,
    )

    assert report.overall_passed is False
    assert report.results[0].network_requests_run2 > 0


def test_cli_data_smoke_help() -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["data", "smoke", "--help"])
    assert res.exit_code == 0
    assert "--symbols" in res.output
