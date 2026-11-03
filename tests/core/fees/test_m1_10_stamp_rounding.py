"""Tests for M1-10: Zerodha stamp-duty rounding to whole rupee and fee schedule verification."""

from __future__ import annotations

import tomllib
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from algodesk.api.trading import Fill, InstrumentId, Product, Side
from algodesk.fees import FeeEngine, Schedule, charges_for_fills


@pytest.fixture
def zerodha_schedule() -> Schedule:
    p = Path(__file__).resolve().parents[3] / "plugins" / "fees" / "zerodha_nse" / "schedule.toml"
    with open(p, "rb") as f:
        data = tomllib.load(f)
    return Schedule.model_validate(data)


def test_schedule_meta_verified_and_source(zerodha_schedule: Schedule) -> None:
    """Requirement 3: schedule meta is marked verified=True with verified_on 2026-11-02."""
    assert zerodha_schedule.meta.verified is True
    assert str(zerodha_schedule.meta.verified_on) == "2026-11-02"
    assert (
        "owner check against Zerodha brokerage calculator, 2 Nov 2026"
        in zerodha_schedule.meta.source
    )


def test_stamp_schedule_dated_fields(zerodha_schedule: Schedule) -> None:
    """Requirement 1: stamp duty components have dated rounding fields and provenance note."""
    effective_dt = date(2026, 11, 2)
    matching_stamps = [
        c
        for c in zerodha_schedule.components
        if c.name == "stamp" and c.effective_from >= effective_dt
    ]
    assert len(matching_stamps) == 6
    for c in matching_stamps:
        assert c.rounding == "whole_rupee_half_up"
        assert c.source == "owner check against Zerodha brokerage calculator, 2 Nov 2026"
        assert c.note == "half-up versus always-up is not yet distinguished"
        assert c.verified is True


def test_delivery_buy_stamp_rounding(zerodha_schedule: Schedule) -> None:
    """Requirement 2: delivery buy of 50,000 gives stamp 8.00 (was 7.50 before 2026-11-02)."""
    turnover = Decimal("50000.00")

    # Before 2026-11-02: unrounded 7.50
    chg_pre = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="eq",
        product=Product.CNC,
        trade_date=date(2026, 11, 1),
        schedule=zerodha_schedule,
        exchange="nse",
    )
    assert chg_pre.stamp == Decimal("7.50")

    # On / after 2026-11-02: rounded half up to whole rupee 8.00
    chg_post = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="eq",
        product=Product.CNC,
        trade_date=date(2026, 11, 2),
        schedule=zerodha_schedule,
        exchange="nse",
    )
    assert chg_post.stamp == Decimal("8.00")


def test_options_buy_stamp_rounding(zerodha_schedule: Schedule) -> None:
    """Requirement 2: options buy of 50,000 gives stamp 2.00 (was 1.50 before 2026-11-02)."""
    turnover = Decimal("50000.00")

    # Before 2026-11-02: unrounded 1.50
    chg_pre = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="fno",
        product=Product.NRML,
        trade_date=date(2026, 11, 1),
        schedule=zerodha_schedule,
        exchange="nse",
        instrument_type="opt",
    )
    assert chg_pre.stamp == Decimal("1.50")

    # On / after 2026-11-02: rounded half up to whole rupee 2.00
    chg_post = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="fno",
        product=Product.NRML,
        trade_date=date(2026, 11, 2),
        schedule=zerodha_schedule,
        exchange="nse",
        instrument_type="opt",
    )
    assert chg_post.stamp == Decimal("2.00")


def test_eight_owner_samples_delivery_nse(zerodha_schedule: Schedule) -> None:
    """Owner sample 1 & 2: Delivery NSE (50k buy + 50k sell).

    Calculator total = 111.74 (+ DP 15.93).
    Engine total excluding DP & GST on DP matches within 1 rupee (diff 0.02).
    """
    turnover = Decimal("50000.00")
    dt = date(2026, 11, 2)

    buy_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="eq",
        product=Product.CNC,
        trade_date=dt,
        schedule=zerodha_schedule,
        exchange="nse",
    )
    assert buy_chg.brokerage == Decimal("0.00")
    assert buy_chg.stt == Decimal("50.00")
    assert buy_chg.exchange_txn == Decimal("1.54")
    assert buy_chg.stamp == Decimal("8.00")
    assert buy_chg.sebi == Decimal("0.05")
    assert buy_chg.dp == Decimal("0.00")
    assert buy_chg.gst == Decimal("0.29")
    assert buy_chg.total == Decimal("59.88")

    sell_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.SELL,
        segment="eq",
        product=Product.CNC,
        trade_date=dt,
        schedule=zerodha_schedule,
        exchange="nse",
    )
    assert sell_chg.brokerage == Decimal("0.00")
    assert sell_chg.stt == Decimal("50.00")
    assert sell_chg.exchange_txn == Decimal("1.54")
    assert sell_chg.stamp == Decimal("0.00")
    assert sell_chg.sebi == Decimal("0.05")
    assert sell_chg.dp == Decimal("13.50")
    assert sell_chg.gst == Decimal("2.72")
    assert sell_chg.total == Decimal("67.81")

    # Combined total with DP
    total_with_dp = buy_chg.total + sell_chg.total
    assert total_with_dp == Decimal("127.69")

    # Calculator excludes DP (13.50) and GST on DP (18% of 13.50 = 2.43) -> DP pkg = 15.93
    dp_component_and_gst = Decimal("13.50") + Decimal("2.43")  # 15.93
    total_without_dp = total_with_dp - dp_component_and_gst
    assert total_without_dp == Decimal("111.76")

    calculator_target = Decimal("111.74")
    diff = abs(total_without_dp - calculator_target)
    assert diff <= Decimal("1.00"), f"Expected diff <= 1.00, got {diff}"
    assert diff == Decimal("0.02")


def test_eight_owner_samples_delivery_bse(zerodha_schedule: Schedule) -> None:
    """Owner sample 3 & 4: Delivery BSE (50k buy + 50k sell).

    Calculator total = 112.54 (+ DP 15.93).
    Engine total excluding DP & GST on DP matches within 1 rupee (diff 0.02).
    """
    turnover = Decimal("50000.00")
    dt = date(2026, 11, 2)

    buy_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="eq",
        product=Product.CNC,
        trade_date=dt,
        schedule=zerodha_schedule,
        exchange="bse",
    )
    assert buy_chg.brokerage == Decimal("0.00")
    assert buy_chg.stt == Decimal("50.00")
    assert buy_chg.exchange_txn == Decimal("1.88")
    assert buy_chg.stamp == Decimal("8.00")
    assert buy_chg.sebi == Decimal("0.05")
    assert buy_chg.dp == Decimal("0.00")
    assert buy_chg.gst == Decimal("0.35")
    assert buy_chg.total == Decimal("60.28")

    sell_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.SELL,
        segment="eq",
        product=Product.CNC,
        trade_date=dt,
        schedule=zerodha_schedule,
        exchange="bse",
    )
    assert sell_chg.brokerage == Decimal("0.00")
    assert sell_chg.stt == Decimal("50.00")
    assert sell_chg.exchange_txn == Decimal("1.88")
    assert sell_chg.stamp == Decimal("0.00")
    assert sell_chg.sebi == Decimal("0.05")
    assert sell_chg.dp == Decimal("13.50")
    assert sell_chg.gst == Decimal("2.78")
    assert sell_chg.total == Decimal("68.21")

    # Combined total with DP
    total_with_dp = buy_chg.total + sell_chg.total
    assert total_with_dp == Decimal("128.49")

    # Calculator excludes DP (13.50) and GST on DP (2.43) -> 15.93
    dp_component_and_gst = Decimal("15.93")
    total_without_dp = total_with_dp - dp_component_and_gst
    assert total_without_dp == Decimal("112.56")

    calculator_target = Decimal("112.54")
    diff = abs(total_without_dp - calculator_target)
    assert diff <= Decimal("1.00"), f"Expected diff <= 1.00, got {diff}"
    assert diff == Decimal("0.02")


def test_eight_owner_samples_options_nse(zerodha_schedule: Schedule) -> None:
    """Owner sample 5 & 6: Options NSE (50k buy + 50k sell).

    Calculator total = 166.24.
    Engine total matches within 1 rupee (diff 0.02).
    """
    turnover = Decimal("50000.00")
    dt = date(2026, 11, 2)

    buy_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="fno",
        product=Product.NRML,
        trade_date=dt,
        schedule=zerodha_schedule,
        exchange="nse",
        instrument_type="opt",
    )
    assert buy_chg.brokerage == Decimal("20.00")
    assert buy_chg.stt == Decimal("0.00")
    assert buy_chg.exchange_txn == Decimal("17.77")
    assert buy_chg.stamp == Decimal("2.00")
    assert buy_chg.sebi == Decimal("0.05")
    assert buy_chg.dp == Decimal("0.00")
    assert buy_chg.gst == Decimal("6.81")
    assert buy_chg.total == Decimal("46.63")

    sell_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.SELL,
        segment="fno",
        product=Product.NRML,
        trade_date=dt,
        schedule=zerodha_schedule,
        exchange="nse",
        instrument_type="opt",
    )
    assert sell_chg.brokerage == Decimal("20.00")
    assert sell_chg.stt == Decimal("75.00")
    assert sell_chg.exchange_txn == Decimal("17.77")
    assert sell_chg.stamp == Decimal("0.00")
    assert sell_chg.sebi == Decimal("0.05")
    assert sell_chg.dp == Decimal("0.00")
    assert sell_chg.gst == Decimal("6.81")
    assert sell_chg.total == Decimal("119.63")

    total = buy_chg.total + sell_chg.total
    assert total == Decimal("166.26")

    calculator_target = Decimal("166.24")
    diff = abs(total - calculator_target)
    assert diff <= Decimal("1.00"), f"Expected diff <= 1.00, got {diff}"
    assert diff == Decimal("0.02")


def test_eight_owner_samples_options_bse(zerodha_schedule: Schedule) -> None:
    """Owner sample 7 & 8: Options BSE (50k buy + 50k sell).

    Calculator total = 162.67.
    Engine total matches within 1 rupee (diff 0.01).
    """
    turnover = Decimal("50000.00")
    dt = date(2026, 11, 2)

    buy_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="fno",
        product=Product.NRML,
        trade_date=dt,
        schedule=zerodha_schedule,
        exchange="bse",
        instrument_type="opt",
    )
    assert buy_chg.brokerage == Decimal("20.00")
    assert buy_chg.stt == Decimal("0.00")
    assert buy_chg.exchange_txn == Decimal("16.25")
    assert buy_chg.stamp == Decimal("2.00")
    assert buy_chg.sebi == Decimal("0.05")
    assert buy_chg.dp == Decimal("0.00")
    assert buy_chg.gst == Decimal("6.53")
    assert buy_chg.total == Decimal("44.83")

    sell_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.SELL,
        segment="fno",
        product=Product.NRML,
        trade_date=dt,
        schedule=zerodha_schedule,
        exchange="bse",
        instrument_type="opt",
    )
    assert sell_chg.brokerage == Decimal("20.00")
    assert sell_chg.stt == Decimal("75.00")
    assert sell_chg.exchange_txn == Decimal("16.25")
    assert sell_chg.stamp == Decimal("0.00")
    assert sell_chg.sebi == Decimal("0.05")
    assert sell_chg.dp == Decimal("0.00")
    assert sell_chg.gst == Decimal("6.53")
    assert sell_chg.total == Decimal("117.83")

    total = buy_chg.total + sell_chg.total
    assert total == Decimal("162.66")

    calculator_target = Decimal("162.67")
    diff = abs(total - calculator_target)
    assert diff <= Decimal("1.00"), f"Expected diff <= 1.00, got {diff}"
    assert diff == Decimal("0.01")


def test_charges_for_fills_stamp_rounding(zerodha_schedule: Schedule) -> None:
    """Verify charges_for_fills computes per-order rounded stamp duty across multiple fills."""
    ts = datetime(2026, 11, 2, 10, 0, 0, tzinfo=UTC)
    fills = [
        Fill(
            fill_id="f1",
            client_ref="ref1",
            instrument=InstrumentId("NSE:EQ:INFY"),
            side=Side.BUY,
            qty=50,
            price=Decimal("1000.00"),  # turnover 50,000 -> stamp 8.00
            ts=ts,
            product=Product.CNC,
        ),
        Fill(
            fill_id="f2",
            client_ref="ref2",
            instrument=InstrumentId("NSE:EQ:TCS"),
            side=Side.BUY,
            qty=10,
            price=Decimal("5000.00"),  # turnover 50,000 -> stamp 8.00
            ts=ts,
            product=Product.CNC,
        ),
    ]

    charges_list = charges_for_fills(fills, zerodha_schedule)
    assert len(charges_list) == 2
    assert charges_list[0].stamp == Decimal("8.00")
    assert charges_list[1].stamp == Decimal("8.00")
    assert charges_list.stamp == Decimal("16.00")
