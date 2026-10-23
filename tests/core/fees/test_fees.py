"""Tests for fee models, FeeEngine, Zerodha NSE schedule, and fill charges."""

from __future__ import annotations

import tomllib
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from algodesk.api.conformance import fees_problems
from algodesk.api.errors import ConfigError
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import InstrumentId
from algodesk.api.trading import Fill, Product, Side
from algodesk.fees import (
    FeeEngine,
    Schedule,
    charges_for_fills,
)


@pytest.fixture
def zerodha_schedule() -> Schedule:
    schedule_path = (
        Path(__file__).resolve().parents[3] / "plugins" / "fees" / "zerodha_nse" / "schedule.toml"
    )
    with open(schedule_path, "rb") as f:
        data = tomllib.load(f)
    return Schedule.model_validate(data)


# ------------------------------------------------------------------------------
# 8 Independent Decimal Fee Vectors
# ------------------------------------------------------------------------------


def test_vector_1_delivery_buy(zerodha_schedule: Schedule) -> None:
    """Vector 1: Equity Delivery BUY (100 @ 500 = 50,000 turnover)."""
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("50000.00"),
        side=Side.BUY,
        segment="eq",
        product=Product.CNC,
        trade_date=date(2026, 10, 26),
        schedule=zerodha_schedule,
    )
    assert chg.brokerage == Decimal("0.00")
    assert chg.stt == Decimal("50.00")  # 0.1%
    assert chg.stamp == Decimal("7.50")  # 0.015%
    assert chg.exchange_txn == Decimal("1.49")  # 0.00297% -> 1.485 -> 1.49
    assert chg.sebi == Decimal("0.05")  # ₹10/cr -> 0.05
    assert chg.dp == Decimal("0.00")
    assert chg.gst == Decimal("0.28")  # 18% of (1.49 + 0.05) = 0.2772 -> 0.28
    assert chg.total == Decimal("59.32")


def test_vector_2_delivery_sell_first_of_day(zerodha_schedule: Schedule) -> None:
    """Vector 2: Equity Delivery SELL first of day (100 @ 500 = 50,000 turnover)."""
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("50000.00"),
        side=Side.SELL,
        segment="eq",
        product=Product.CNC,
        trade_date=date(2026, 10, 26),
        schedule=zerodha_schedule,
        is_first_scrip_day=True,
    )
    assert chg.brokerage == Decimal("0.00")
    assert chg.stt == Decimal("50.00")  # 0.1%
    assert chg.stamp == Decimal("0.00")
    assert chg.exchange_txn == Decimal("1.49")
    assert chg.sebi == Decimal("0.05")
    assert chg.dp == Decimal("13.50")  # DP charge ₹13.50
    assert chg.gst == Decimal("2.71")  # 18% of (1.49 + 0.05 + 13.50) = 2.7072 -> 2.71
    assert chg.total == Decimal("67.75")


def test_vector_3_delivery_sell_second_trade(zerodha_schedule: Schedule) -> None:
    """Vector 3: Equity Delivery SELL second of day (50 @ 500 = 25,000 turnover)."""
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("25000.00"),
        side=Side.SELL,
        segment="eq",
        product=Product.CNC,
        trade_date=date(2026, 10, 26),
        schedule=zerodha_schedule,
        is_first_scrip_day=False,
    )
    assert chg.brokerage == Decimal("0.00")
    assert chg.stt == Decimal("25.00")
    assert chg.stamp == Decimal("0.00")
    assert chg.exchange_txn == Decimal("0.74")  # 25000 * 0.0000297 = 0.7425 -> 0.74
    assert chg.sebi == Decimal("0.03")  # 25000 * 10 / 1e7 = 0.025 -> 0.03
    assert chg.dp == Decimal("0.00")  # Suppressed!
    assert chg.gst == Decimal("0.14")  # 18% of (0.74 + 0.03) = 0.1386 -> 0.14
    assert chg.total == Decimal("25.91")


def test_vector_4_intraday_buy(zerodha_schedule: Schedule) -> None:
    """Vector 4: Equity Intraday BUY (10 @ 2,500 = 25,000 turnover)."""
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("25000.00"),
        side=Side.BUY,
        segment="eq",
        product=Product.MIS,
        trade_date=date(2026, 10, 26),
        schedule=zerodha_schedule,
    )
    assert chg.brokerage == Decimal("7.50")  # 0.03% of 25000 = 7.50
    assert chg.stt == Decimal("0.00")  # No STT on intraday buy
    assert chg.stamp == Decimal("0.75")  # 0.003% of 25000 = 0.75
    assert chg.exchange_txn == Decimal("0.74")
    assert chg.sebi == Decimal("0.03")
    assert chg.dp == Decimal("0.00")
    assert chg.gst == Decimal("1.49")  # 18% of (7.50 + 0.74 + 0.03) = 1.4886 -> 1.49
    assert chg.total == Decimal("10.51")


def test_vector_5_intraday_sell_accept_trade(zerodha_schedule: Schedule) -> None:
    """Vector 5: Equity Intraday SELL Accept trade (10 @ 2,500 = 25,000 turnover)."""
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("25000.00"),
        side=Side.SELL,
        segment="eq",
        product=Product.MIS,
        trade_date=date(2026, 10, 26),
        schedule=zerodha_schedule,
    )
    assert chg.brokerage == Decimal("7.50")
    assert chg.stt == Decimal("6.25")  # 0.025% of 25000 = 6.25
    assert chg.stamp == Decimal("0.00")
    assert chg.exchange_txn == Decimal("0.74")
    assert chg.sebi == Decimal("0.03")
    assert chg.dp == Decimal("0.00")
    assert chg.gst == Decimal("1.49")
    assert chg.total == Decimal("16.01")


def test_vector_6_intraday_large_buy_capped(zerodha_schedule: Schedule) -> None:
    """Vector 6: Intraday large BUY with capped brokerage (1,000 @ 500 = 500,000 turnover)."""
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("500000.00"),
        side=Side.BUY,
        segment="eq",
        product=Product.MIS,
        trade_date=date(2026, 10, 26),
        schedule=zerodha_schedule,
    )
    assert chg.brokerage == Decimal("20.00")  # 0.03% is ₹150 -> capped at ₹20.00
    assert chg.stt == Decimal("0.00")
    assert chg.stamp == Decimal("15.00")  # 0.003% of 500,000 = 15.00
    assert chg.exchange_txn == Decimal("14.85")  # 500,000 * 0.0000297 = 14.85
    assert chg.sebi == Decimal("0.50")  # 500,000 * 10 / 1e7 = 0.50
    assert chg.gst == Decimal("6.36")  # 18% of (20.00 + 14.85 + 0.50) = 6.363 -> 6.36
    assert chg.total == Decimal("56.71")


def test_vector_7_futures_buy(zerodha_schedule: Schedule) -> None:
    """Vector 7: Futures BUY (250 @ 2,000 = 500,000 turnover)."""
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("500000.00"),
        side=Side.BUY,
        segment="fno",
        product=Product.NRML,
        trade_date=date(2026, 10, 26),
        schedule=zerodha_schedule,
    )
    assert chg.brokerage == Decimal("20.00")  # Capped at ₹20
    assert chg.stt == Decimal("0.00")
    assert chg.stamp == Decimal("10.00")  # 0.002% unverified stamp duty = 10.00
    assert chg.exchange_txn == Decimal("8.65")  # 500,000 * 0.0000173 = 8.65
    assert chg.sebi == Decimal("0.50")
    assert chg.gst == Decimal("5.25")  # 18% of (20.00 + 8.65 + 0.50) = 5.247 -> 5.25
    assert chg.total == Decimal("44.40")


def test_vector_8_futures_sell(zerodha_schedule: Schedule) -> None:
    """Vector 8: Futures SELL (250 @ 2,000 = 500,000 turnover)."""
    chg = FeeEngine.calculate_trade_charges(
        turnover=Decimal("500000.00"),
        side=Side.SELL,
        segment="fno",
        product=Product.NRML,
        trade_date=date(2026, 10, 26),
        schedule=zerodha_schedule,
    )
    assert chg.brokerage == Decimal("20.00")
    assert chg.stt == Decimal("100.00")  # 0.02% of 500,000 = 100.00
    assert chg.stamp == Decimal("0.00")
    assert chg.exchange_txn == Decimal("8.65")
    assert chg.sebi == Decimal("0.50")
    assert chg.gst == Decimal("5.25")
    assert chg.total == Decimal("134.40")


# ------------------------------------------------------------------------------
# DP Charge Once per Scrip/Day in charges_for_fills
# ------------------------------------------------------------------------------


def test_charges_for_fills_dp_once_per_scrip_day(zerodha_schedule: Schedule) -> None:
    ts_day1 = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)
    ts_day2 = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)

    inst_rel = InstrumentId("NSE:EQ:RELIANCE")
    inst_tcs = InstrumentId("NSE:EQ:TCS")

    fills = [
        # RELIANCE sell 1 (day 1) -> DP charged
        Fill(
            "f1",
            "c1",
            10,
            Decimal("2500"),
            ts_day1,
            instrument=inst_rel,
            side=Side.SELL,
            product=Product.CNC,
        ),
        # RELIANCE sell 2 (day 1) -> DP suppressed
        Fill(
            "f2",
            "c2",
            5,
            Decimal("2500"),
            ts_day1,
            instrument=inst_rel,
            side=Side.SELL,
            product=Product.CNC,
        ),
        # TCS sell 1 (day 1) -> DP charged
        Fill(
            "f3",
            "c3",
            10,
            Decimal("3500"),
            ts_day1,
            instrument=inst_tcs,
            side=Side.SELL,
            product=Product.CNC,
        ),
        # RELIANCE sell 3 (day 2) -> new day, DP charged
        Fill(
            "f4",
            "c4",
            10,
            Decimal("2500"),
            ts_day2,
            instrument=inst_rel,
            side=Side.SELL,
            product=Product.CNC,
        ),
    ]

    results = charges_for_fills(fills, zerodha_schedule)
    assert len(results) == 4

    assert results[0].dp == Decimal("13.50")
    assert results[1].dp == Decimal("0.00")
    assert results[2].dp == Decimal("13.50")
    assert results[3].dp == Decimal("13.50")

    # Aggregate convenience properties
    assert results.dp == Decimal("40.50")  # 3 * 13.50
    assert results.total == sum((c.total for c in results), Decimal("0.00"))


def test_missing_segment_or_product_raises_config_error(zerodha_schedule: Schedule) -> None:
    with pytest.raises(ConfigError, match="No fee schedule components found"):
        FeeEngine.calculate_trade_charges(
            turnover=Decimal("1000"),
            side=Side.BUY,
            segment="crypto",
            product=Product.CNC,
            trade_date=date(2026, 10, 26),
            schedule=zerodha_schedule,
        )

    with pytest.raises(ConfigError, match="No fee schedule components found"):
        FeeEngine.calculate_trade_charges(
            turnover=Decimal("1000"),
            side=Side.BUY,
            segment="eq",
            product="UNKNOWN",  # type: ignore[arg-type]
            trade_date=date(2026, 10, 26),
            schedule=zerodha_schedule,
        )


def test_fees_conformance_checks() -> None:
    manifest = PluginManifest(
        name="zerodha_nse",
        kind="fees",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="plugin:ZerodhaNseFeesPlugin",
        capabilities={"equity_delivery": True, "equity_intraday": True, "futures": True},
    )

    class GoodFees:
        def __init__(self) -> None:
            self.manifest = manifest

        def schedule(self) -> dict[str, Any]:
            return {"meta": {"name": "test"}, "component": []}

    assert fees_problems(GoodFees()) == []

    # Bad object missing schedule()
    class BadFees:
        def __init__(self) -> None:
            self.manifest = manifest

    assert any("must define callable schedule()" in p for p in fees_problems(BadFees()))
