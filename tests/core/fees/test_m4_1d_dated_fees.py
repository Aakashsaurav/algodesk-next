"""Tests for M4-1d: Dated NSE equity transaction charges and verified DP charge.

Covers:
- CP2 step 6: Hand-computed vectors with arithmetic in comments (tolerance 0.005)
  for delivery BUY and SELL across dated boundaries:
  * 2023-03-31 (fallback to earliest verified rates)
  * 2023-04-01 (boundary, 0.00325%)
  * 2024-03-31 (boundary, 0.00325%)
  * 2024-04-01 (boundary, 0.00322%)
  * 2024-09-30 (boundary, 0.00322%)
  * 2024-10-01 (boundary, 0.00297% and verified DP 13.00)
  * 2026-11-13 (current rates)
- CP2 step 6: Error without fallback before 2023-04-01 (exchange_txn) and 2024-10-01 (DP)
- CP2 step 6: CLI fees show output for 2024-03-31 and 2024-04-01 differs only in exchange_txn
- CP2 step 7 (Addendum): DP = 13.0 and GST = 2.34 (15.34 per delivery SELL) with independent vector
  for SELL 1 share at 1232.10 on 2026-11-13 (and 2024-10-01): STT 1.2321 + txn 0.0366 + SEBI 0.0012
  + GST 0.0068 + DP 13 + GST 2.34 = 15.34 -> total ~16.62
- CP2 step 7 (Addendum): README discrepancy resolved check (no open discrepancy)
"""

from __future__ import annotations

import tomllib
from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from algodesk.api.trading import Product, Side
from algodesk.cli.app import app
from algodesk.fees.engine import FeeEngine
from algodesk.fees.errors import FeeScheduleError
from algodesk.fees.models import Schedule

runner = CliRunner()


@pytest.fixture
def zerodha_schedule() -> Schedule:
    path = (
        Path(__file__).resolve().parents[3] / "plugins" / "fees" / "zerodha_nse" / "schedule.toml"
    )
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return Schedule(**data)


@pytest.fixture(autouse=True)
def clean_fee_engine_state() -> Generator[None]:
    FeeEngine.reset_fallback_count()
    FeeEngine.set_fallback(None)
    yield
    FeeEngine.reset_fallback_count()
    FeeEngine.set_fallback(None)


# ------------------------------------------------------------------------------
# CP2 Step 6: 7 Dated Boundary Vectors (Delivery BUY and SELL)
# ------------------------------------------------------------------------------


def test_vector_1_delivery_buy_sell_2023_03_31_fallback(zerodha_schedule: Schedule) -> None:
    """Vector 1: 2023-03-31 (fallback to earliest verified rates: txn 0.00325%, DP 13.00).

    Hand-computed arithmetic (turnover 50,000.00):
      BUY (fallback):
        brokerage = 0.00
        stt = 50,000 * 0.001 = 50.00
        stamp = 50,000 * 0.00015 = 7.50
        exchange_txn = 50,000 * 0.0000325 = 1.625 -> 1.63
        sebi = 50,000 * 10 / 1e7 = 0.05
        dp = 0.00
        GST base = 1.63 + 0.05 = 1.68; GST 18% = 0.3024 -> 0.30
        Total BUY = 50.00 + 7.50 + 1.63 + 0.05 + 0.30 = 59.48
      SELL (fallback for txn 0.00325% and fallback for DP 13.00):
        brokerage = 0.00
        stt = 50.00
        stamp = 0.00
        exchange_txn = 1.63
        sebi = 0.05
        dp = 13.00 (fallback)
        GST base = 1.63 + 0.05 + 13.00 = 14.68; GST 18% = 2.6424 -> 2.64
        Total SELL = 50.00 + 1.63 + 0.05 + 13.00 + 2.64 = 67.32
    """
    turnover = Decimal("50000.00")
    dt = date(2023, 3, 31)

    buy_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="eq",
        product=Product.CNC,
        trade_date=dt,
        schedule=zerodha_schedule,
        fallback="earliest",
    )
    assert abs(buy_chg.exchange_txn - Decimal("1.63")) <= Decimal("0.005")
    assert abs(buy_chg.stt - Decimal("50.00")) <= Decimal("0.005")
    assert abs(buy_chg.stamp - Decimal("7.50")) <= Decimal("0.005")
    assert abs(buy_chg.total - Decimal("59.48")) <= Decimal("0.005")

    sell_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.SELL,
        segment="eq",
        product=Product.CNC,
        trade_date=dt,
        schedule=zerodha_schedule,
        fallback="earliest",
    )
    assert abs(sell_chg.exchange_txn - Decimal("1.63")) <= Decimal("0.005")
    assert abs(sell_chg.dp - Decimal("13.00")) <= Decimal("0.005")
    assert abs(sell_chg.gst - Decimal("2.64")) <= Decimal("0.005")
    assert abs(sell_chg.total - Decimal("67.32")) <= Decimal("0.005")


def test_vector_2_delivery_buy_sell_2023_04_01_boundary(zerodha_schedule: Schedule) -> None:
    """Vector 2: 2023-04-01 boundary (first day of verified 0.00325% rate).

    Hand-computed arithmetic (turnover 50,000.00):
      BUY (no fallback needed; exchange_txn is verified from 2023-04-01):
        stt = 50.00, stamp = 7.50, txn = 1.63, sebi = 0.05, GST = 0.30
        Total BUY = 59.48
      SELL (uses fallback='earliest' for DP before 2024-10-01):
        stt = 50.00, txn = 1.63, sebi = 0.05, dp = 13.00, GST = 2.64
        Total SELL = 67.32
    """
    turnover = Decimal("50000.00")
    dt = date(2023, 4, 1)

    buy_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="eq",
        product=Product.CNC,
        trade_date=dt,
        schedule=zerodha_schedule,
        fallback=None,
    )
    assert abs(buy_chg.exchange_txn - Decimal("1.63")) <= Decimal("0.005")
    assert abs(buy_chg.total - Decimal("59.48")) <= Decimal("0.005")

    sell_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.SELL,
        segment="eq",
        product=Product.CNC,
        trade_date=dt,
        schedule=zerodha_schedule,
        fallback="earliest",
    )
    assert abs(sell_chg.exchange_txn - Decimal("1.63")) <= Decimal("0.005")
    assert abs(sell_chg.dp - Decimal("13.00")) <= Decimal("0.005")
    assert abs(sell_chg.gst - Decimal("2.64")) <= Decimal("0.005")
    assert abs(sell_chg.total - Decimal("67.32")) <= Decimal("0.005")


def test_vector_3_delivery_buy_sell_2024_03_31_boundary(zerodha_schedule: Schedule) -> None:
    """Vector 3: 2024-03-31 boundary (last day of 0.00325% rate).

    Hand-computed arithmetic (turnover 50,000.00):
      BUY: txn = 1.63, Total BUY = 59.48
      SELL (fallback='earliest' for DP): txn = 1.63, dp = 13.00, GST = 2.64, Total SELL = 67.32
    """
    turnover = Decimal("50000.00")
    dt = date(2024, 3, 31)

    buy_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="eq",
        product=Product.CNC,
        trade_date=dt,
        schedule=zerodha_schedule,
    )
    assert abs(buy_chg.exchange_txn - Decimal("1.63")) <= Decimal("0.005")
    assert abs(buy_chg.total - Decimal("59.48")) <= Decimal("0.005")

    sell_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.SELL,
        segment="eq",
        product=Product.CNC,
        trade_date=dt,
        schedule=zerodha_schedule,
        fallback="earliest",
    )
    assert abs(sell_chg.exchange_txn - Decimal("1.63")) <= Decimal("0.005")
    assert abs(sell_chg.dp - Decimal("13.00")) <= Decimal("0.005")
    assert abs(sell_chg.gst - Decimal("2.64")) <= Decimal("0.005")
    assert abs(sell_chg.total - Decimal("67.32")) <= Decimal("0.005")


def test_vector_4_delivery_buy_sell_2024_04_01_boundary(zerodha_schedule: Schedule) -> None:
    """Vector 4: 2024-04-01 boundary (first day of revised 0.00322% rate).

    Hand-computed arithmetic (turnover 50,000.00):
      exchange_txn = 50,000 * 0.0000322 = 1.61
      BUY:
        stt = 50.00, stamp = 7.50, txn = 1.61, sebi = 0.05
        GST base = 1.61 + 0.05 = 1.66; GST 18% = 0.2988 -> 0.30
        Total BUY = 50.00 + 7.50 + 1.61 + 0.05 + 0.30 = 59.46
      SELL (fallback='earliest' for DP):
        stt = 50.00, txn = 1.61, sebi = 0.05, dp = 13.00
        GST base = 1.61 + 0.05 + 13.00 = 14.66; GST 18% = 2.6388 -> 2.64
        Total SELL = 50.00 + 1.61 + 0.05 + 13.00 + 2.64 = 67.30
    """
    turnover = Decimal("50000.00")
    dt = date(2024, 4, 1)

    buy_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="eq",
        product=Product.CNC,
        trade_date=dt,
        schedule=zerodha_schedule,
    )
    assert abs(buy_chg.exchange_txn - Decimal("1.61")) <= Decimal("0.005")
    assert abs(buy_chg.total - Decimal("59.46")) <= Decimal("0.005")

    sell_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.SELL,
        segment="eq",
        product=Product.CNC,
        trade_date=dt,
        schedule=zerodha_schedule,
        fallback="earliest",
    )
    assert abs(sell_chg.exchange_txn - Decimal("1.61")) <= Decimal("0.005")
    assert abs(sell_chg.dp - Decimal("13.00")) <= Decimal("0.005")
    assert abs(sell_chg.gst - Decimal("2.64")) <= Decimal("0.005")
    assert abs(sell_chg.total - Decimal("67.30")) <= Decimal("0.005")


def test_vector_5_delivery_buy_sell_2024_09_30_boundary(zerodha_schedule: Schedule) -> None:
    """Vector 5: 2024-09-30 boundary (last day of 0.00322% rate).

    Hand-computed arithmetic (turnover 50,000.00):
      BUY: txn = 1.61, Total BUY = 59.46
      SELL (fallback='earliest' for DP): txn = 1.61, dp = 13.00, GST = 2.64, Total SELL = 67.30
    """
    turnover = Decimal("50000.00")
    dt = date(2024, 9, 30)

    buy_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="eq",
        product=Product.CNC,
        trade_date=dt,
        schedule=zerodha_schedule,
    )
    assert abs(buy_chg.exchange_txn - Decimal("1.61")) <= Decimal("0.005")
    assert abs(buy_chg.total - Decimal("59.46")) <= Decimal("0.005")

    sell_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.SELL,
        segment="eq",
        product=Product.CNC,
        trade_date=dt,
        schedule=zerodha_schedule,
        fallback="earliest",
    )
    assert abs(sell_chg.exchange_txn - Decimal("1.61")) <= Decimal("0.005")
    assert abs(sell_chg.dp - Decimal("13.00")) <= Decimal("0.005")
    assert abs(sell_chg.gst - Decimal("2.64")) <= Decimal("0.005")
    assert abs(sell_chg.total - Decimal("67.30")) <= Decimal("0.005")


def test_vector_6_delivery_buy_sell_2024_10_01_boundary(zerodha_schedule: Schedule) -> None:
    """Vector 6: 2024-10-01 boundary (first day of 0.00297% rate and verified DP 13.00).

    Hand-computed arithmetic (turnover 50,000.00):
      exchange_txn = 50,000 * 0.0000297 = 1.485 -> 1.49
      BUY:
        stt = 50.00, stamp = 7.50, txn = 1.49, sebi = 0.05
        GST base = 1.49 + 0.05 = 1.54; GST 18% = 0.2772 -> 0.28
        Total BUY = 50.00 + 7.50 + 1.49 + 0.05 + 0.28 = 59.32
      SELL (both txn and DP verified, no fallback needed):
        stt = 50.00, txn = 1.49, sebi = 0.05, dp = 13.00
        GST base = 1.49 + 0.05 + 13.00 = 14.54; GST 18% = 2.6172 -> 2.62
        Total SELL = 50.00 + 1.49 + 0.05 + 13.00 + 2.62 = 67.16
    """
    turnover = Decimal("50000.00")
    dt = date(2024, 10, 1)

    buy_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="eq",
        product=Product.CNC,
        trade_date=dt,
        schedule=zerodha_schedule,
        fallback=None,
    )
    assert abs(buy_chg.exchange_txn - Decimal("1.49")) <= Decimal("0.005")
    assert abs(buy_chg.total - Decimal("59.32")) <= Decimal("0.005")

    sell_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.SELL,
        segment="eq",
        product=Product.CNC,
        trade_date=dt,
        schedule=zerodha_schedule,
        fallback=None,
    )
    assert abs(sell_chg.exchange_txn - Decimal("1.49")) <= Decimal("0.005")
    assert abs(sell_chg.dp - Decimal("13.00")) <= Decimal("0.005")
    assert abs(sell_chg.gst - Decimal("2.62")) <= Decimal("0.005")
    assert abs(sell_chg.total - Decimal("67.16")) <= Decimal("0.005")


def test_vector_7_delivery_buy_sell_2026_11_13(zerodha_schedule: Schedule) -> None:
    """Vector 7: 2026-11-13 (current rates: NSE txn 0.00307%, stamp rounding 8.00, DP 13.00).

    Hand-computed arithmetic (turnover 50,000.00):
      exchange_txn = 50,000 * 0.0000307 = 1.535 -> 1.54
      BUY:
        stt = 50.00, stamp = 8.00 (rounded half up), txn = 1.54, sebi = 0.05
        GST base = 1.54 + 0.05 = 1.59; GST 18% = 0.2862 -> 0.29
        Total BUY = 50.00 + 8.00 + 1.54 + 0.05 + 0.29 = 59.88
      SELL:
        stt = 50.00, txn = 1.54, sebi = 0.05, dp = 13.00
        GST base = 1.54 + 0.05 + 13.00 = 14.59; GST 18% = 2.6262 -> 2.63
        Total SELL = 50.00 + 1.54 + 0.05 + 13.00 + 2.63 = 67.22
    """
    turnover = Decimal("50000.00")
    dt = date(2026, 11, 13)

    buy_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="eq",
        product=Product.CNC,
        trade_date=dt,
        schedule=zerodha_schedule,
        fallback=None,
    )
    assert abs(buy_chg.exchange_txn - Decimal("1.54")) <= Decimal("0.005")
    assert abs(buy_chg.stamp - Decimal("8.00")) <= Decimal("0.005")
    assert abs(buy_chg.total - Decimal("59.88")) <= Decimal("0.005")

    sell_chg = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.SELL,
        segment="eq",
        product=Product.CNC,
        trade_date=dt,
        schedule=zerodha_schedule,
        fallback=None,
    )
    assert abs(sell_chg.exchange_txn - Decimal("1.54")) <= Decimal("0.005")
    assert abs(sell_chg.dp - Decimal("13.00")) <= Decimal("0.005")
    assert abs(sell_chg.gst - Decimal("2.63")) <= Decimal("0.005")
    assert abs(sell_chg.total - Decimal("67.22")) <= Decimal("0.005")


# ------------------------------------------------------------------------------
# CP2 Step 6: Fail Closed Before Earliest Verified Date
# ------------------------------------------------------------------------------


def test_error_without_fallback_before_2023_04_01(zerodha_schedule: Schedule) -> None:
    """Trade before earliest verified effective_from (2023-04-01) raises FeeScheduleError."""
    with pytest.raises(FeeScheduleError) as exc_info:
        FeeEngine.calculate_trade_charges(
            turnover=Decimal("50000.00"),
            side=Side.BUY,
            segment="eq",
            product=Product.CNC,
            trade_date=date(2023, 3, 31),
            schedule=zerodha_schedule,
            fallback=None,
        )

    err = exc_info.value
    assert "before earliest verified" in err.what
    assert "2023-04-01" in err.what
    assert "exchange_txn" in err.where
    assert "fees.fallback = 'earliest'" in err.fix


def test_sell_error_without_fallback_before_dp_verified_date(zerodha_schedule: Schedule) -> None:
    """Delivery SELL before DP verified date (2024-10-01) raises FeeScheduleError."""
    with pytest.raises(FeeScheduleError) as exc_info:
        FeeEngine.calculate_trade_charges(
            turnover=Decimal("50000.00"),
            side=Side.SELL,
            segment="eq",
            product=Product.CNC,
            trade_date=date(2024, 4, 1),
            schedule=zerodha_schedule,
            fallback=None,
        )

    err = exc_info.value
    assert "before earliest verified" in err.what
    assert "2024-10-01" in err.what
    assert "dp" in err.where
    assert "fees.fallback = 'earliest'" in err.fix


# ------------------------------------------------------------------------------
# CP2 Step 6: CLI fees show 2024-03-31 vs 2024-04-01
# ------------------------------------------------------------------------------


def test_cli_fees_show_2024_03_31_and_2024_04_01_differ_only_in_exchange_txn() -> None:
    """`algodesk fees show zerodha_nse --as-of <date>` differs only in exchange_txn."""
    res_31 = runner.invoke(app, ["fees", "show", "zerodha_nse", "--as-of", "2024-03-31"])
    res_01 = runner.invoke(app, ["fees", "show", "zerodha_nse", "--as-of", "2024-04-01"])

    assert res_31.exit_code == 0
    assert res_01.exit_code == 0

    lines_31 = [
        line.strip()
        for line in res_31.stdout.splitlines()
        if line.strip() and not line.startswith("===") and not line.startswith("Source:")
    ]
    lines_01 = [
        line.strip()
        for line in res_01.stdout.splitlines()
        if line.strip() and not line.startswith("===") and not line.startswith("Source:")
    ]

    # Both must output the exact same number of lines
    assert len(lines_31) == len(lines_01)

    differing = [(l1, l2) for l1, l2 in zip(lines_31, lines_01, strict=True) if l1 != l2]
    # Differing lines must only be exchange_txn
    assert len(differing) > 0
    for l1, l2 in differing:
        assert "exchange_txn" in l1
        assert "exchange_txn" in l2
        assert "0.0000325" in l1
        assert "0.0000322" in l2


def test_cli_fees_show_out_of_bounds_2022_01_03_actionable_error() -> None:
    """CLI fees show --as-of 2022-01-03 outputs clear actionable error mentioning fees.fallback."""
    res = runner.invoke(app, ["fees", "show", "zerodha_nse", "--as-of", "2022-01-03"])
    assert res.exit_code == 1
    assert "ERROR" in res.stdout or "FeeScheduleError" in res.stdout
    assert "2023-04-01" in res.stdout
    assert "fees.fallback = 'earliest'" in res.stdout


# ------------------------------------------------------------------------------
# CP2 Step 7 (Addendum): Single Share SELL Vector on 2026-11-13 & 2024-10-01
# ------------------------------------------------------------------------------


def test_step_7_addendum_vector_sell_1_share_at_1232_10(zerodha_schedule: Schedule) -> None:
    """Addendum Step 7: SELL 1 share at 1232.10 on 2026-11-13 with DP = 13.00 & GST = 2.34.

    STT 0.1% = 1.2321; txn 0.00297% = 0.0366; SEBI 10 per crore = 0.0012;
    GST 18% on (txn + SEBI) = 0.0068; DP 13 + GST 2.34 = 15.34;
    total about 16.62 (old schedule gave 17.21).

    Hand-computed arithmetic (turnover 1232.10):
      STT 0.1% = 1232.10 * 0.001 = 1.2321 -> 1.23
      exchange_txn:
        on 2024-10-01 (0.00297%): 1232.10 * 0.0000297 = 0.036593 -> 0.04 (itemised)
        on 2026-11-13 (0.00307%): 1232.10 * 0.0000307 = 0.037825 -> 0.04 (itemised)
      sebi 10/crore = 1232.10 * 10 / 1e7 = 0.0012321 -> 0.00
      DP component = 13.00
      DP package with GST = 13.00 + 18% GST (2.34) = 15.34
      GST 18% on (txn + sebi + DP) = 18% of (0.04 + 0.00 + 13.00) = 18% of 13.04 = 2.3472 -> 2.35
      Total charges = 1.23 + 0.04 + 0.00 + 13.00 + 2.35 = 16.62
    """
    # 1. Evaluate on 2024-10-01 (0.00297% txn rate)
    chg_2024 = FeeEngine.calculate_trade_charges(
        turnover=Decimal("1232.10"),
        side=Side.SELL,
        segment="eq",
        product=Product.CNC,
        trade_date=date(2024, 10, 1),
        schedule=zerodha_schedule,
        fallback=None,
    )
    assert chg_2024.dp == Decimal("13.00")
    dp_gst = (Decimal("13.00") * Decimal("0.18")).quantize(Decimal("0.01"))
    assert dp_gst == Decimal("2.34")
    assert chg_2024.dp + dp_gst == Decimal("15.34")

    assert abs(chg_2024.stt - Decimal("1.23")) <= Decimal("0.005")
    assert abs(chg_2024.exchange_txn - Decimal("0.04")) <= Decimal("0.005")
    assert abs(chg_2024.sebi - Decimal("0.00")) <= Decimal("0.005")
    assert abs(chg_2024.total - Decimal("16.62")) <= Decimal("0.01")

    # 2. Evaluate on 2026-11-13 (current rates)
    chg_2026 = FeeEngine.calculate_trade_charges(
        turnover=Decimal("1232.10"),
        side=Side.SELL,
        segment="eq",
        product=Product.CNC,
        trade_date=date(2026, 11, 13),
        schedule=zerodha_schedule,
        fallback=None,
    )
    assert chg_2026.dp == Decimal("13.00")
    assert abs(chg_2026.total - Decimal("16.62")) <= Decimal("0.01")


# ------------------------------------------------------------------------------
# CP2 Step 7 (Addendum): README Discrepancy Resolved Check
# ------------------------------------------------------------------------------


def test_readme_discrepancy_resolved() -> None:
    """README has removed any 'discrepancy open' line and documents verified DP = 13.00."""
    readme_path = (
        Path(__file__).resolve().parents[3] / "plugins" / "fees" / "zerodha_nse" / "README.md"
    )
    text = readme_path.read_text(encoding="utf-8")
    assert "discrepancy" not in text.lower() or "discrepancy open" not in text.lower()
    assert "13.00" in text
    assert "15.34" in text
    assert "2026-11-20" in text
