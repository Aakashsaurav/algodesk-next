"""Unit and integration tests for fill arithmetic and trades.csv session date formatting."""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from algodesk.api.market import Bar, InstrumentId
from algodesk.data.bar_synth import BarQuoteSynth
from algodesk.engine.backtest import BacktestRunner


def test_fill_1_arithmetic_next_bar_low_and_slippage() -> None:
    """Verify FILL-1 price: 9 Jan 2025 Low (1258.25) + 5 bps slippage rounded to 0.05 tick."""
    low_price = Decimal("1258.25")
    slippage_bps = Decimal("5")  # 5 bps = 0.0005
    multiplier = Decimal("1") + (slippage_bps / Decimal("10000"))
    raw_fill = low_price * multiplier
    assert raw_fill == Decimal("1258.879125")

    # Round to nearest 0.05 tick
    tick = Decimal("0.05")
    rounded = (raw_fill / tick).quantize(Decimal("1")) * tick
    assert rounded == Decimal("1258.90")


def test_bar_quote_synth_daily_bar_timestamps() -> None:
    """BarQuoteSynth divides a 24-hr daily bar into 4 quotes at start, +6h, +12h, and end."""
    synth = BarQuoteSynth(participation_pct=10.0, price_tick=0.05)
    s_ts = datetime(2025, 1, 8, 18, 30, 0, tzinfo=UTC)  # 9 Jan 00:00 IST
    e_ts = datetime(2025, 1, 9, 18, 30, 0, tzinfo=UTC)  # 9 Jan 24:00 IST

    bar = Bar(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        timeframe="1d",
        start_ts=s_ts,
        end_ts=e_ts,
        open=1267.0,
        high=1272.95,
        low=1258.25,
        close=1259.05,
        volume=50000.0,
    )

    q0, q1, q2, q3 = synth.synthesize(bar)
    # Quote 0 is at start_ts (00:00 IST)
    assert q0.recv_ts == s_ts
    assert q0.ltp == 1267.0

    # Quote 1 is at start_ts + 6h (06:00 IST)
    assert q1.recv_ts == s_ts + timedelta(hours=6)
    # Down bar (close < open): order is O, H, L, C, so q1 is high or q2 is low
    # Close 1259.05 < Open 1267.0, so prices are [Open, High, Low, Close]
    assert q1.ltp == 1272.95
    assert q2.ltp == 1258.25
    assert q2.recv_ts == s_ts + timedelta(hours=12)


def test_normalize_trades_session_dates_formats_ts_ist(tmp_path: Path) -> None:
    """trades.csv ts_ist is formatted as YYYY-MM-DD 00:00:00 IST preserving 10 columns."""
    res_dir = tmp_path / "results_run"
    res_dir.mkdir(parents=True, exist_ok=True)
    trades_csv = res_dir / "trades.csv"

    headers = [
        "trade_id",
        "fill_id",
        "client_ref",
        "instrument",
        "side",
        "qty",
        "price",
        "charges",
        "ts",
        "ts_ist",
    ]
    rows = [
        {
            "trade_id": "FILL-1",
            "fill_id": "FILL-1",
            "client_ref": "ref-1",
            "instrument": "NSE:EQ:RELIANCE",
            "side": "BUY",
            "qty": "1",
            "price": "1258.9",
            "charges": "1.5",
            "ts": "2025-01-09T00:30:00+00:00",
            "ts_ist": "2025-01-09 06:00:00 IST",
        },
        {
            "trade_id": "FILL-2",
            "fill_id": "FILL-2",
            "client_ref": "ref-2",
            "instrument": "NSE:EQ:RELIANCE",
            "side": "SELL",
            "qty": "1",
            "price": "1232.1",
            "charges": "17.21",
            "ts": "2025-01-29T00:30:00+00:00",
            "ts_ist": "2025-01-29 06:00:00 IST",
        },
    ]

    with open(trades_csv, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)

    # Instantiate mock runner with 1d timeframe
    class MockConfig:
        class backtest:
            timeframe = "1d"

    class MockSessionRunner:
        run_config = MockConfig()

    class MockBacktestRunner(BacktestRunner):
        def __init__(self) -> None:
            self.session_runner = MockSessionRunner()

    mock_runner = MockBacktestRunner()
    mock_runner._normalize_trades_session_dates(res_dir)

    with open(trades_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert list(reader.fieldnames or []) == headers
        read_rows = list(reader)

    assert len(read_rows) == 2
    assert read_rows[0]["ts_ist"] == "2025-01-09 00:00:00 IST"
    assert read_rows[1]["ts_ist"] == "2025-01-29 00:00:00 IST"
    # UTC timestamps and prices must remain unchanged
    assert read_rows[0]["ts"] == "2025-01-09T00:30:00+00:00"
    assert read_rows[0]["price"] == "1258.9"


def test_normalize_trades_leaves_intraday_untouched(tmp_path: Path) -> None:
    """trades.csv for 1m intraday runs preserves exact intra-day timestamp."""
    res_dir = tmp_path / "results_run_1m"
    res_dir.mkdir(parents=True, exist_ok=True)
    trades_csv = res_dir / "trades.csv"

    headers = [
        "trade_id",
        "fill_id",
        "client_ref",
        "instrument",
        "side",
        "qty",
        "price",
        "charges",
        "ts",
        "ts_ist",
    ]
    rows = [
        {
            "trade_id": "FILL-1",
            "fill_id": "FILL-1",
            "client_ref": "ref-1",
            "instrument": "NSE:EQ:RELIANCE",
            "side": "BUY",
            "qty": "1",
            "price": "1258.9",
            "charges": "1.5",
            "ts": "2025-01-09T04:00:00+00:00",
            "ts_ist": "2025-01-09 09:30:00 IST",
        }
    ]
    with open(trades_csv, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)

    class MockConfig:
        class backtest:
            timeframe = "1m"

    class MockSessionRunner:
        run_config = MockConfig()

    class MockBacktestRunner(BacktestRunner):
        def __init__(self) -> None:
            self.session_runner = MockSessionRunner()

    mock_runner = MockBacktestRunner()
    mock_runner._normalize_trades_session_dates(res_dir)

    with open(trades_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        read_rows = list(reader)

    assert read_rows[0]["ts_ist"] == "2025-01-09 09:30:00 IST"
