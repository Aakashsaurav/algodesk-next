"""Unit and integration tests for fill arithmetic, open-quote rule, and trades.csv quote time."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from plugins.venues.sim.venue import SimVenue

from algodesk.api.market import Bar, InstrumentId
from algodesk.api.trading import OrderSpec, OrderState, OrderType, Side
from algodesk.data.bar_synth import BarQuoteSynth
from algodesk.engine.backtest import BacktestRunner


def _round_to_tick(val: Decimal, tick: Decimal) -> Decimal:
    return (val / tick).quantize(Decimal("1")) * tick


@pytest.mark.parametrize(
    ("side", "expected_raw", "expected_rounded"),
    [
        (Side.BUY, Decimal("1267.6335"), Decimal("1267.65")),
        (Side.SELL, Decimal("1266.3665"), Decimal("1266.35")),
    ],
)
def test_fill_arithmetic_next_bar_open_and_slippage_table(
    side: Side,
    expected_raw: Decimal,
    expected_rounded: Decimal,
) -> None:
    """Step 6 Table test: Market BUY and SELL on bar t fill from bar t+1 OPEN +/- slippage."""
    open_price = Decimal("1267.00")
    slippage_bps = Decimal("5")  # 5 bps = 0.0005
    slip_factor = slippage_bps / Decimal("10000")

    if side == Side.BUY:
        raw_fill = open_price * (Decimal("1") + slip_factor)
    else:
        raw_fill = open_price * (Decimal("1") - slip_factor)

    assert raw_fill == expected_raw
    tick = Decimal("0.05")
    rounded = _round_to_tick(raw_fill, tick)
    assert rounded == expected_rounded


@pytest.mark.anyio
async def test_step1_fixture_execution_fills_at_open_quote() -> None:
    """Step 6 End-to-end: BUY and SELL orders decided on bar t fill from bar t+1 OPEN."""
    inst = InstrumentId("NSE:EQ:RELIANCE")
    # Bar t: 8 Jan 2025
    b_t = Bar(
        instrument=inst,
        timeframe="1d",
        start_ts=datetime(2025, 1, 7, 18, 30, tzinfo=UTC),
        end_ts=datetime(2025, 1, 8, 18, 30, tzinfo=UTC),
        open=1265.00,
        high=1275.00,
        low=1262.00,
        close=1272.00,
        volume=60000.0,
    )
    # Bar t+1: 9 Jan 2025 (Down bar: O 1267.00, H 1269.75, L 1248.05, C 1254.75)
    b_next = Bar(
        instrument=inst,
        timeframe="1d",
        start_ts=datetime(2025, 1, 8, 18, 30, tzinfo=UTC),
        end_ts=datetime(2025, 1, 9, 18, 30, tzinfo=UTC),
        open=1267.00,
        high=1269.75,
        low=1248.05,
        close=1254.75,
        volume=70000.0,
    )

    synth = BarQuoteSynth(participation_pct=10.0, price_tick=0.05)
    quotes_t = synth.synthesize(b_t)
    quotes_next = synth.synthesize(b_next)

    # 1. BUY Market Order
    venue_buy = SimVenue(params={"mode": "backtest", "backtest.slippage_bps": 5})
    venue_buy.configure([inst])
    for q in quotes_t:
        venue_buy.on_quote(q)

    spec_buy = OrderSpec(instrument=inst, side=Side.BUY, qty=10, type=OrderType.MARKET)
    await venue_buy.place(spec_buy, "buy_1")

    for q in quotes_next:
        venue_buy.on_quote(q)

    st_buy = venue_buy.order_status("buy_1")
    assert st_buy is not None
    assert st_buy.state == OrderState.FILLED
    fill_buy = venue_buy._orders["buy_1"].gross_fills[0]
    # Fills from OPEN quote (1267.00) + 5 bps = 1267.65
    assert fill_buy.price == Decimal("1267.65")
    assert fill_buy.ts == quotes_next[0].recv_ts
    assert fill_buy.ts == datetime(2025, 1, 9, 3, 45, 0, tzinfo=UTC)

    # 2. SELL Market Order
    venue_sell = SimVenue(params={"mode": "backtest", "backtest.slippage_bps": 5})
    venue_sell.allow_short = True
    venue_sell.configure([inst])
    for q in quotes_t:
        venue_sell.on_quote(q)

    spec_sell = OrderSpec(instrument=inst, side=Side.SELL, qty=10, type=OrderType.MARKET)
    await venue_sell.place(spec_sell, "sell_1")

    for q in quotes_next:
        venue_sell.on_quote(q)

    st_sell = venue_sell.order_status("sell_1")
    assert st_sell is not None
    assert st_sell.state == OrderState.FILLED
    fill_sell = venue_sell._orders["sell_1"].gross_fills[0]
    # Fills from OPEN quote (1267.00) - 5 bps = 1266.35
    assert fill_sell.price == Decimal("1266.35")
    assert fill_sell.ts == quotes_next[0].recv_ts
    assert fill_sell.ts == datetime(2025, 1, 9, 3, 45, 0, tzinfo=UTC)


def test_bar_quote_synth_daily_bar_timestamps() -> None:
    """BarQuoteSynth maps a 24-hr daily bar to session open (09:15 IST) and session close."""
    synth = BarQuoteSynth(participation_pct=10.0, price_tick=0.05)
    s_ts = datetime(2025, 1, 8, 18, 30, 0, tzinfo=UTC)  # 9 Jan 00:00 IST
    e_ts = datetime(2025, 1, 9, 18, 30, 0, tzinfo=UTC)  # 9 Jan 24:00 IST

    bar = Bar(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        timeframe="1d",
        start_ts=s_ts,
        end_ts=e_ts,
        open=1267.0,
        high=1269.75,
        low=1248.05,
        close=1254.75,
        volume=50000.0,
    )

    q0, q1, q2, q3 = synth.synthesize(bar)
    # Quote 0 is at exchange session open (09:15:00 IST / 03:45:00 UTC)
    expected_open_utc = datetime(2025, 1, 9, 3, 45, 0, tzinfo=UTC)
    expected_close_utc = datetime(2025, 1, 9, 10, 0, 0, tzinfo=UTC)

    assert q0.recv_ts == expected_open_utc
    assert q0.ltp == 1267.0

    # Quotes 1 and 2 are inside the session
    assert expected_open_utc < q1.recv_ts < q2.recv_ts < expected_close_utc
    # Down bar (close < open): order is O, H, L, C
    assert q1.ltp == 1269.75
    assert q2.ltp == 1248.05

    # Quote 3 is at session close (15:30:00 IST / 10:00:00 UTC)
    assert q3.recv_ts == expected_close_utc
    assert q3.ltp == 1254.75


def test_normalize_trades_session_dates_preserves_quote_time(tmp_path: Path) -> None:
    """trades.csv ts_ist shows full date and time (09:15:00 IST) matching real quote time."""
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
            "price": "1267.65",
            "charges": "1.5",
            "ts": "2025-01-09T03:45:00+00:00",
            "ts_ist": "2025-01-09 09:15:00 IST",
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
            "ts": "2025-01-29T03:45:00+00:00",
            "ts_ist": "2025-01-29 09:15:00 IST",
        },
    ]

    with open(trades_csv, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)

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
    # ts_ist shows full date and time 09:15:00 IST (real quote time), NOT 00:00:00 IST
    assert read_rows[0]["ts_ist"] == "2025-01-09 09:15:00 IST"
    assert read_rows[1]["ts_ist"] == "2025-01-29 09:15:00 IST"
    assert read_rows[0]["ts"] == "2025-01-09T03:45:00+00:00"
    assert read_rows[0]["price"] == "1267.65"


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
            "price": "1267.65",
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
