"""Tests for M1-8 fee schedule updates, STT revisions, options charges, and exchange routing."""

from __future__ import annotations

import tomllib
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from algodesk.api.errors import NotSupported
from algodesk.api.trading import Fill, InstrumentId, Product, Side
from algodesk.fees import FeeEngine, Schedule, charges_for_fills


@pytest.fixture
def zerodha_schedule() -> Schedule:
    p = Path(__file__).resolve().parents[3] / "plugins" / "fees" / "zerodha_nse" / "schedule.toml"
    with open(p, "rb") as f:
        data = tomllib.load(f)
    return Schedule.model_validate(data)


def test_schedule_meta_unverified_and_source(zerodha_schedule: Schedule) -> None:
    assert zerodha_schedule.meta.verified is False
    assert "zerodha.com/charges" in zerodha_schedule.meta.source
    assert "1 Apr 2026 STT notice" in zerodha_schedule.meta.source


def test_calendar_nse_verified() -> None:
    p = (
        Path(__file__).resolve().parents[3]
        / "plugins"
        / "calendars"
        / "exchange"
        / "data"
        / "nse.toml"
    )
    with open(p, "rb") as f:
        data = tomllib.load(f)
    assert data.get("verified") is True
    assert data.get("source") == "owner cross-check against the NSE circular"
    assert str(data.get("verified_on")) == "2026-10-30"


def test_equity_delivery_nse_vs_bse_post_2026_10_30(zerodha_schedule: Schedule) -> None:
    turnover = Decimal("100000.00")
    dt = date(2026, 10, 30)

    # NSE delivery buy
    nse_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="eq",
        product=Product.CNC,
        trade_date=dt,
        schedule=zerodha_schedule,
        exchange="nse",
    )
    assert nse_chg.exchange_txn == Decimal("3.07")  # 0.00307% of 100,000

    # BSE delivery buy
    bse_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="eq",
        product=Product.CNC,
        trade_date=dt,
        schedule=zerodha_schedule,
        exchange="bse",
    )
    assert bse_chg.exchange_txn == Decimal("3.75")  # 0.00375% of 100,000


def test_equity_intraday_nse_vs_bse_post_2026_10_30(zerodha_schedule: Schedule) -> None:
    turnover = Decimal("50000.00")
    dt = date(2026, 10, 30)

    nse_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="eq",
        product=Product.MIS,
        trade_date=dt,
        schedule=zerodha_schedule,
        exchange="nse",
    )
    assert nse_chg.exchange_txn == Decimal("1.54")  # 0.00307% of 50,000 -> 1.535 -> 1.54

    bse_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="eq",
        product=Product.MIS,
        trade_date=dt,
        schedule=zerodha_schedule,
        exchange="bse",
    )
    assert bse_chg.exchange_txn == Decimal("1.88")  # 0.00375% of 50,000 -> 1.875 -> 1.88


def test_futures_stt_transition(zerodha_schedule: Schedule) -> None:
    turnover = Decimal("1000000.00")

    # Before 2026-04-01: STT 0.02%
    pre_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.SELL,
        segment="fno",
        product=Product.NRML,
        trade_date=date(2026, 3, 31),
        schedule=zerodha_schedule,
        instrument_type="fut",
    )
    assert pre_chg.stt == Decimal("200.00")  # 0.02% of 1,000,000

    # From 2026-04-01: STT 0.05%
    post_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.SELL,
        segment="fno",
        product=Product.NRML,
        trade_date=date(2026, 4, 1),
        schedule=zerodha_schedule,
        instrument_type="fut",
    )
    assert post_chg.stt == Decimal("500.00")  # 0.05% of 1,000,000


def test_futures_txn_charges_nse_vs_bse(zerodha_schedule: Schedule) -> None:
    turnover = Decimal("1000000.00")
    dt = date(2026, 10, 30)

    # NSE futures txn charge: 0.00183%
    nse_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="fno",
        product=Product.NRML,
        trade_date=dt,
        schedule=zerodha_schedule,
        exchange="nse",
        instrument_type="fut",
    )
    assert nse_chg.exchange_txn == Decimal("18.30")

    # BSE futures txn charge: 0.0%
    bse_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="fno",
        product=Product.NRML,
        trade_date=dt,
        schedule=zerodha_schedule,
        exchange="bse",
        instrument_type="fut",
    )
    assert bse_chg.exchange_txn == Decimal("0.00")


def test_options_stt_transition(zerodha_schedule: Schedule) -> None:
    turnover = Decimal("100000.00")

    # Before 2026-04-01: STT 0.0625%
    pre_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.SELL,
        segment="fno",
        product=Product.NRML,
        trade_date=date(2026, 3, 31),
        schedule=zerodha_schedule,
        instrument_type="opt",
    )
    assert pre_chg.stt == Decimal("62.50")  # 0.0625% of 100,000

    # From 2026-04-01: STT 0.15%
    post_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.SELL,
        segment="fno",
        product=Product.NRML,
        trade_date=date(2026, 4, 1),
        schedule=zerodha_schedule,
        instrument_type="opt",
    )
    assert post_chg.stt == Decimal("150.00")  # 0.15% of 100,000


def test_options_txn_charges_nse_vs_bse(zerodha_schedule: Schedule) -> None:
    turnover = Decimal("100000.00")
    dt = date(2026, 10, 30)

    # NSE options txn charge: 0.03553% of premium
    nse_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="fno",
        product=Product.NRML,
        trade_date=dt,
        schedule=zerodha_schedule,
        exchange="nse",
        instrument_type="opt",
    )
    assert nse_chg.exchange_txn == Decimal("35.53")

    # BSE options txn charge: 0.0325% of premium
    bse_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="fno",
        product=Product.NRML,
        trade_date=dt,
        schedule=zerodha_schedule,
        exchange="bse",
        instrument_type="opt",
    )
    assert bse_chg.exchange_txn == Decimal("32.50")


def test_options_brokerage_is_flat_20(zerodha_schedule: Schedule) -> None:
    dt = date(2026, 10, 30)
    for prod in (Product.NRML, Product.MIS):
        for side in (Side.BUY, Side.SELL):
            chg = FeeEngine.calculate_trade_charges(
                turnover=Decimal("200.00"),
                side=side,
                segment="fno",
                product=prod,
                trade_date=dt,
                schedule=zerodha_schedule,
                exchange="nse",
                instrument_type="opt",
            )
            assert chg.brokerage == Decimal("20.00")


def test_options_stamp_duty_buy_only(zerodha_schedule: Schedule) -> None:
    turnover = Decimal("100000.00")
    dt = date(2026, 10, 30)

    buy_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="fno",
        product=Product.NRML,
        trade_date=dt,
        schedule=zerodha_schedule,
        instrument_type="opt",
    )
    assert buy_chg.stamp == Decimal("3.00")  # 0.003% of 100,000

    sell_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.SELL,
        segment="fno",
        product=Product.NRML,
        trade_date=dt,
        schedule=zerodha_schedule,
        instrument_type="opt",
    )
    assert sell_chg.stamp == Decimal("0.00")


def test_options_exercise_raises_not_supported(zerodha_schedule: Schedule) -> None:
    with pytest.raises(NotSupported, match="Options exercise pricing is not supported"):
        FeeEngine.calculate_trade_charges(
            turnover=Decimal("50000.00"),
            side=Side.SELL,
            segment="fno",
            product=Product.NRML,
            trade_date=date(2026, 10, 30),
            schedule=zerodha_schedule,
            is_exercise=True,
        )


def test_charges_for_fills_automatic_fno_type_resolution(zerodha_schedule: Schedule) -> None:
    from datetime import UTC, datetime

    fut_fill = Fill(
        fill_id="fill_fut_1",
        client_ref="ref_1",
        instrument=InstrumentId("NSE:FNO:NIFTY26OCTFUT"),
        side=Side.SELL,
        qty=50,
        price=Decimal("24000.00"),
        ts=datetime(2026, 10, 30, 9, 30, tzinfo=UTC),
        product=Product.NRML,
    )

    opt_fill = Fill(
        fill_id="fill_opt_1",
        client_ref="ref_2",
        instrument=InstrumentId("NSE:FNO:NIFTY26OCT24000CE"),
        side=Side.SELL,
        qty=50,
        price=Decimal("150.00"),
        ts=datetime(2026, 10, 30, 9, 31, tzinfo=UTC),
        product=Product.NRML,
    )

    results = charges_for_fills([fut_fill, opt_fill], zerodha_schedule)
    assert len(results) == 2

    # Futures fill turnover = 50 * 24000 = 1,200,000
    # Futures STT = 0.05% of 1,200,000 = 600.00
    assert results[0].stt == Decimal("600.00")

    # Options fill turnover = 50 * 150 = 7,500
    # Options STT = 0.15% of 7,500 = 11.25
    assert results[1].stt == Decimal("11.25")
    assert results[1].brokerage == Decimal("20.00")


def test_vector_1_nse_delivery_buy_itemised(zerodha_schedule: Schedule) -> None:
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("50000.00"),
        side=Side.BUY,
        segment="eq",
        product=Product.CNC,
        trade_date=date(2026, 10, 30),
        schedule=zerodha_schedule,
        exchange="nse",
    )
    assert chg.brokerage == Decimal("0.00")
    assert chg.stt == Decimal("50.00")
    assert chg.exchange_txn == Decimal("1.54")
    assert chg.stamp == Decimal("7.50")
    assert chg.sebi == Decimal("0.05")
    assert chg.dp == Decimal("0.00")
    assert chg.gst == Decimal("0.29")
    assert chg.total == Decimal("59.38")


def test_vector_2_nse_delivery_sell_itemised(zerodha_schedule: Schedule) -> None:
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("50000.00"),
        side=Side.SELL,
        segment="eq",
        product=Product.CNC,
        trade_date=date(2026, 10, 30),
        schedule=zerodha_schedule,
        exchange="nse",
        is_first_scrip_day=True,
    )
    assert chg.brokerage == Decimal("0.00")
    assert chg.stt == Decimal("50.00")
    assert chg.exchange_txn == Decimal("1.54")
    assert chg.stamp == Decimal("0.00")
    assert chg.sebi == Decimal("0.05")
    assert chg.dp == Decimal("13.50")
    assert chg.gst == Decimal("2.72")
    assert chg.total == Decimal("67.81")


def test_vector_3_bse_delivery_buy_itemised(zerodha_schedule: Schedule) -> None:
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("50000.00"),
        side=Side.BUY,
        segment="eq",
        product=Product.CNC,
        trade_date=date(2026, 10, 30),
        schedule=zerodha_schedule,
        exchange="bse",
    )
    assert chg.brokerage == Decimal("0.00")
    assert chg.stt == Decimal("50.00")
    assert chg.exchange_txn == Decimal("1.88")
    assert chg.stamp == Decimal("7.50")
    assert chg.sebi == Decimal("0.05")
    assert chg.dp == Decimal("0.00")
    assert chg.gst == Decimal("0.35")
    assert chg.total == Decimal("59.78")


def test_vector_4_bse_delivery_sell_itemised(zerodha_schedule: Schedule) -> None:
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("50000.00"),
        side=Side.SELL,
        segment="eq",
        product=Product.CNC,
        trade_date=date(2026, 10, 30),
        schedule=zerodha_schedule,
        exchange="bse",
        is_first_scrip_day=True,
    )
    assert chg.brokerage == Decimal("0.00")
    assert chg.stt == Decimal("50.00")
    assert chg.exchange_txn == Decimal("1.88")
    assert chg.stamp == Decimal("0.00")
    assert chg.sebi == Decimal("0.05")
    assert chg.dp == Decimal("13.50")
    assert chg.gst == Decimal("2.78")
    assert chg.total == Decimal("68.21")


def test_vector_5_nse_intraday_buy_itemised(zerodha_schedule: Schedule) -> None:
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("25000.00"),
        side=Side.BUY,
        segment="eq",
        product=Product.MIS,
        trade_date=date(2026, 10, 30),
        schedule=zerodha_schedule,
        exchange="nse",
    )
    assert chg.brokerage == Decimal("7.50")
    assert chg.stt == Decimal("0.00")
    assert chg.exchange_txn == Decimal("0.77")
    assert chg.stamp == Decimal("0.75")
    assert chg.sebi == Decimal("0.03")
    assert chg.dp == Decimal("0.00")
    assert chg.gst == Decimal("1.49")
    assert chg.total == Decimal("10.54")


def test_vector_6_nse_intraday_sell_itemised(zerodha_schedule: Schedule) -> None:
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("25000.00"),
        side=Side.SELL,
        segment="eq",
        product=Product.MIS,
        trade_date=date(2026, 10, 30),
        schedule=zerodha_schedule,
        exchange="nse",
    )
    assert chg.brokerage == Decimal("7.50")
    assert chg.stt == Decimal("6.25")
    assert chg.exchange_txn == Decimal("0.77")
    assert chg.stamp == Decimal("0.00")
    assert chg.sebi == Decimal("0.03")
    assert chg.dp == Decimal("0.00")
    assert chg.gst == Decimal("1.49")
    assert chg.total == Decimal("16.04")


def test_vector_7_bse_intraday_buy_itemised(zerodha_schedule: Schedule) -> None:
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("25000.00"),
        side=Side.BUY,
        segment="eq",
        product=Product.MIS,
        trade_date=date(2026, 10, 30),
        schedule=zerodha_schedule,
        exchange="bse",
    )
    assert chg.brokerage == Decimal("7.50")
    assert chg.stt == Decimal("0.00")
    assert chg.exchange_txn == Decimal("0.94")
    assert chg.stamp == Decimal("0.75")
    assert chg.sebi == Decimal("0.03")
    assert chg.gst == Decimal("1.52")
    assert chg.total == Decimal("10.74")


def test_vector_8_bse_intraday_sell_itemised(zerodha_schedule: Schedule) -> None:
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("25000.00"),
        side=Side.SELL,
        segment="eq",
        product=Product.MIS,
        trade_date=date(2026, 10, 30),
        schedule=zerodha_schedule,
        exchange="bse",
    )
    assert chg.brokerage == Decimal("7.50")
    assert chg.stt == Decimal("6.25")
    assert chg.exchange_txn == Decimal("0.94")
    assert chg.stamp == Decimal("0.00")
    assert chg.sebi == Decimal("0.03")
    assert chg.gst == Decimal("1.52")
    assert chg.total == Decimal("16.24")


def test_vector_9_nse_futures_buy_itemised(zerodha_schedule: Schedule) -> None:
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("500000.00"),
        side=Side.BUY,
        segment="fno",
        product=Product.NRML,
        trade_date=date(2026, 10, 30),
        schedule=zerodha_schedule,
        exchange="nse",
        instrument_type="fut",
    )
    assert chg.brokerage == Decimal("20.00")
    assert chg.stt == Decimal("0.00")
    assert chg.exchange_txn == Decimal("9.15")
    assert chg.stamp == Decimal("10.00")
    assert chg.sebi == Decimal("0.50")
    assert chg.gst == Decimal("5.34")
    assert chg.total == Decimal("44.99")


def test_vector_10_nse_futures_sell_itemised(zerodha_schedule: Schedule) -> None:
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("500000.00"),
        side=Side.SELL,
        segment="fno",
        product=Product.NRML,
        trade_date=date(2026, 10, 30),
        schedule=zerodha_schedule,
        exchange="nse",
        instrument_type="fut",
    )
    assert chg.brokerage == Decimal("20.00")
    assert chg.stt == Decimal("250.00")
    assert chg.exchange_txn == Decimal("9.15")
    assert chg.stamp == Decimal("0.00")
    assert chg.sebi == Decimal("0.50")
    assert chg.gst == Decimal("5.34")
    assert chg.total == Decimal("284.99")


def test_vector_11_bse_futures_buy_itemised(zerodha_schedule: Schedule) -> None:
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("500000.00"),
        side=Side.BUY,
        segment="fno",
        product=Product.NRML,
        trade_date=date(2026, 10, 30),
        schedule=zerodha_schedule,
        exchange="bse",
        instrument_type="fut",
    )
    assert chg.brokerage == Decimal("20.00")
    assert chg.stt == Decimal("0.00")
    assert chg.exchange_txn == Decimal("0.00")
    assert chg.stamp == Decimal("10.00")
    assert chg.sebi == Decimal("0.50")
    assert chg.gst == Decimal("3.69")
    assert chg.total == Decimal("34.19")


def test_vector_12_bse_futures_sell_itemised(zerodha_schedule: Schedule) -> None:
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("500000.00"),
        side=Side.SELL,
        segment="fno",
        product=Product.NRML,
        trade_date=date(2026, 10, 30),
        schedule=zerodha_schedule,
        exchange="bse",
        instrument_type="fut",
    )
    assert chg.brokerage == Decimal("20.00")
    assert chg.stt == Decimal("250.00")
    assert chg.exchange_txn == Decimal("0.00")
    assert chg.stamp == Decimal("0.00")
    assert chg.sebi == Decimal("0.50")
    assert chg.gst == Decimal("3.69")
    assert chg.total == Decimal("274.19")


def test_vector_13_nse_options_buy_itemised(zerodha_schedule: Schedule) -> None:
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("50000.00"),
        side=Side.BUY,
        segment="fno",
        product=Product.NRML,
        trade_date=date(2026, 10, 30),
        schedule=zerodha_schedule,
        exchange="nse",
        instrument_type="opt",
    )
    assert chg.brokerage == Decimal("20.00")
    assert chg.stt == Decimal("0.00")
    assert chg.exchange_txn == Decimal("17.77")
    assert chg.stamp == Decimal("1.50")
    assert chg.sebi == Decimal("0.05")
    assert chg.gst == Decimal("6.81")
    assert chg.total == Decimal("46.13")


def test_vector_14_nse_options_sell_itemised(zerodha_schedule: Schedule) -> None:
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("50000.00"),
        side=Side.SELL,
        segment="fno",
        product=Product.NRML,
        trade_date=date(2026, 10, 30),
        schedule=zerodha_schedule,
        exchange="nse",
        instrument_type="opt",
    )
    assert chg.brokerage == Decimal("20.00")
    assert chg.stt == Decimal("75.00")
    assert chg.exchange_txn == Decimal("17.77")
    assert chg.stamp == Decimal("0.00")
    assert chg.sebi == Decimal("0.05")
    assert chg.gst == Decimal("6.81")
    assert chg.total == Decimal("119.63")


def test_vector_15_bse_options_buy_itemised(zerodha_schedule: Schedule) -> None:
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("50000.00"),
        side=Side.BUY,
        segment="fno",
        product=Product.NRML,
        trade_date=date(2026, 10, 30),
        schedule=zerodha_schedule,
        exchange="bse",
        instrument_type="opt",
    )
    assert chg.brokerage == Decimal("20.00")
    assert chg.stt == Decimal("0.00")
    assert chg.exchange_txn == Decimal("16.25")
    assert chg.stamp == Decimal("1.50")
    assert chg.sebi == Decimal("0.05")
    assert chg.gst == Decimal("6.53")
    assert chg.total == Decimal("44.33")


def test_vector_16_bse_options_sell_itemised(zerodha_schedule: Schedule) -> None:
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("50000.00"),
        side=Side.SELL,
        segment="fno",
        product=Product.NRML,
        trade_date=date(2026, 10, 30),
        schedule=zerodha_schedule,
        exchange="bse",
        instrument_type="opt",
    )
    assert chg.brokerage == Decimal("20.00")
    assert chg.stt == Decimal("75.00")
    assert chg.exchange_txn == Decimal("16.25")
    assert chg.stamp == Decimal("0.00")
    assert chg.sebi == Decimal("0.05")
    assert chg.gst == Decimal("6.53")
    assert chg.total == Decimal("117.83")
