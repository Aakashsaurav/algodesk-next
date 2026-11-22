"""Tests for M4-6: MCX commodity futures declared fee schedule and vector verification.

Requirements: FR-FEE-1, FR-FEE-2, FR-FEE-3, FR-FEE-4, FR-FEE-5; ADR-092.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import tomli

from algodesk.api.trading import Fill, Product, Side
from algodesk.fees.engine import FeeEngine, _coerce_schedule, charges_for_fills
from algodesk.fees.mapping import resolve_fee_segment_and_product
from algodesk.fees.models import Schedule
from algodesk.ops.fees import inspect_fee_schedule
from algodesk.results.writer import ResultsWriter


@pytest.fixture
def zerodha_schedule() -> Schedule:
    root_dir = Path(__file__).resolve().parents[3]
    with open(root_dir / "plugins" / "fees" / "zerodha_nse" / "schedule.toml", "rb") as f:
        return _coerce_schedule(tomli.load(f))


def test_mcx_commodity_futures_vector_buy_10l_mis(zerodha_schedule: Schedule) -> None:
    """Vector 1: MCX commodity futures BUY turnover Rs 10,00,000, MIS.

    Hand-computed arithmetic (turnover Rs 10,00,000 per fill, MIS):
      turnover = 1,000,000.00
      brokerage = min(0.03% x 1,000,000 = 300, 20) = 20.00
      exchange txn (MCX) = 0.0021% x 1,000,000 = 21.00
      SEBI turnover (non-agri) = Rs 10 / crore x 1,000,000 = 1.00
      GST = 18% x (brokerage + exchange txn + SEBI) = 18% x (20 + 21 + 1) = 18% x 42 = 7.56
      stamp duty = 0.002% x 1,000,000 = 20.00
      CTT = 0.00 (sell side only)
      Total BUY = 20.00 + 21.00 + 1.00 + 7.56 + 20.00 = 69.56
    """
    trade_dt = date(2026, 11, 13)
    turnover = Decimal("1000000.00")

    charges = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="com",
        product=Product.MIS,
        trade_date=trade_dt,
        schedule=zerodha_schedule,
        exchange="mcx",
        instrument_type="fut",
        instrument="MCX:FUT:GOLD:2026-12-04",
    )

    tolerance = Decimal("0.005")
    assert abs(charges.brokerage - Decimal("20.00")) <= tolerance
    assert abs(charges.exchange_txn - Decimal("21.00")) <= tolerance
    assert abs(charges.sebi - Decimal("1.00")) <= tolerance
    assert abs(charges.gst - Decimal("7.56")) <= tolerance
    assert abs(charges.stamp - Decimal("20.00")) <= tolerance
    assert abs(charges.items.get("ctt", Decimal("0.00")) - Decimal("0.00")) <= tolerance
    assert abs(charges.total - Decimal("69.56")) <= tolerance


def test_mcx_commodity_futures_vector_sell_10l_mis(zerodha_schedule: Schedule) -> None:
    """Vector 2: MCX commodity futures SELL turnover Rs 10,00,000, MIS.

    Hand-computed arithmetic (turnover Rs 10,00,000 per fill, MIS):
      turnover = 1,000,000.00
      brokerage = min(0.03% x 1,000,000 = 300, 20) = 20.00
      exchange txn (MCX) = 0.0021% x 1,000,000 = 21.00
      SEBI turnover (non-agri) = Rs 10 / crore x 1,000,000 = 1.00
      GST = 18% x (brokerage + exchange txn + SEBI) = 18% x (20 + 21 + 1) = 18% x 42 = 7.56
      CTT = 0.01% x 1,000,000 = 100.00 (non-agri sell side)
      stamp duty = 0.00 (buy side only)
      Total SELL = 20.00 + 21.00 + 1.00 + 7.56 + 100.00 = 149.56
    """
    trade_dt = date(2026, 11, 13)
    turnover = Decimal("1000000.00")

    charges = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.SELL,
        segment="com",
        product=Product.MIS,
        trade_date=trade_dt,
        schedule=zerodha_schedule,
        exchange="mcx",
        instrument_type="fut",
        instrument="MCX:FUT:GOLD:2026-12-04",
    )

    tolerance = Decimal("0.005")
    assert abs(charges.brokerage - Decimal("20.00")) <= tolerance
    assert abs(charges.exchange_txn - Decimal("21.00")) <= tolerance
    assert abs(charges.sebi - Decimal("1.00")) <= tolerance
    assert abs(charges.gst - Decimal("7.56")) <= tolerance
    assert abs(charges.items["ctt"] - Decimal("100.00")) <= tolerance
    assert abs(charges.stamp - Decimal("0.00")) <= tolerance
    assert abs(charges.total - Decimal("149.56")) <= tolerance


def test_mcx_commodity_futures_small_trade_50k_brokerage(zerodha_schedule: Schedule) -> None:
    """Vector 3: Small trade (turnover Rs 50,000) where 0.03% is below Rs 20 uses 15.00 brokerage.

    Hand-computed arithmetic (turnover Rs 50,000.00):
      brokerage = min(0.03% x 50,000 = 15.00, 20.00) = 15.00
      exchange txn = 0.0021% x 50,000 = 1.05
      SEBI = 10 / 10,000,000 x 50,000 = 0.05
      GST = 18% x (15.00 + 1.05 + 0.05 = 16.10) = 2.898 -> round_half_up = 2.90
      stamp duty = 0.002% x 50,000 = 1.00
      Total BUY = 15.00 + 1.05 + 0.05 + 2.90 + 1.00 = 20.00
    """
    trade_dt = date(2026, 11, 13)
    turnover = Decimal("50000.00")

    charges = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="com",
        product=Product.MIS,
        trade_date=trade_dt,
        schedule=zerodha_schedule,
        exchange="mcx",
        instrument_type="fut",
        instrument="MCX:FUT:GOLD:2026-12-04",
    )

    tolerance = Decimal("0.005")
    assert abs(charges.brokerage - Decimal("15.00")) <= tolerance
    assert abs(charges.exchange_txn - Decimal("1.05")) <= tolerance
    assert abs(charges.sebi - Decimal("0.05")) <= tolerance
    assert abs(charges.gst - Decimal("2.90")) <= tolerance
    assert abs(charges.stamp - Decimal("1.00")) <= tolerance
    assert abs(charges.total - Decimal("20.00")) <= tolerance


def test_mcx_commodity_futures_nrml_matches_mis(zerodha_schedule: Schedule) -> None:
    """NRML product defines identical rate components to MIS."""
    trade_dt = date(2026, 11, 13)
    turnover = Decimal("1000000.00")

    buy_nrml = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.BUY,
        segment="com",
        product=Product.NRML,
        trade_date=trade_dt,
        schedule=zerodha_schedule,
        exchange="mcx",
        instrument_type="fut",
        instrument="MCX:FUT:GOLD:2026-12-04",
    )
    sell_nrml = FeeEngine.calculate_trade_charges(
        turnover=turnover,
        side=Side.SELL,
        segment="com",
        product=Product.NRML,
        trade_date=trade_dt,
        schedule=zerodha_schedule,
        exchange="mcx",
        instrument_type="fut",
        instrument="MCX:FUT:GOLD:2026-12-04",
    )

    tolerance = Decimal("0.005")
    assert abs(buy_nrml.total - Decimal("69.56")) <= tolerance
    assert abs(sell_nrml.total - Decimal("149.56")) <= tolerance


def test_regression_nse_equity_and_futures_unchanged(zerodha_schedule: Schedule) -> None:
    """Regression vector: NSE equity fill and NSE futures fill remain unchanged."""
    trade_dt = date(2026, 11, 13)

    # 1. NSE Equity Delivery (turnover 10,000 CNC)
    # brokerage = 0.00, stt = 10.00, stamp = 2.00, exchange_txn = 0.31, sebi = 0.01, gst = 0.06
    # Total = 12.38
    eq_charges = FeeEngine.calculate_trade_charges(
        turnover=Decimal("10000.00"),
        side=Side.BUY,
        segment="eq",
        product=Product.CNC,
        trade_date=trade_dt,
        schedule=zerodha_schedule,
        exchange="nse",
        instrument="NSE:EQ:RELIANCE",
    )
    assert abs(eq_charges.total - Decimal("12.38")) <= Decimal("0.005")

    # 2. NSE Equity Futures (turnover 1,000,000 MIS SELL)
    # brokerage = 20.00, stt = 500.00 (0.05% post-hike), exchange_txn = 18.30 (0.00183%),
    # sebi = 1.00, gst = 18% x (20 + 18.30 + 1) = 7.07
    # Total = 546.37
    fut_charges = FeeEngine.calculate_trade_charges(
        turnover=Decimal("1000000.00"),
        side=Side.SELL,
        segment="fno",
        product=Product.MIS,
        trade_date=trade_dt,
        schedule=zerodha_schedule,
        exchange="nse",
        instrument_type="fut",
        instrument="NSE:FUT:NIFTY",
    )
    assert abs(fut_charges.total - Decimal("546.37")) <= Decimal("0.005")


def test_fees_show_lists_commodity_components(tmp_path: Path) -> None:
    """`algodesk fees show zerodha_nse --as-of 2026-11-13` resolves commodity components."""
    from algodesk.cli.common import open_runtime

    root_dir = Path(__file__).resolve().parents[3]
    with open_runtime(root=root_dir, ensure=False) as rt:
        info = inspect_fee_schedule(rt, "zerodha_nse", as_of="2026-11-13")
        active = info["active_comps"]
        com_comps = [c for c in active if c.segment.lower() == "com"]
        assert len(com_comps) == 10

        comp_names = {c.name for c in com_comps}
        assert comp_names == {"brokerage", "ctt", "exchange_txn", "sebi", "stamp"}

        mcx_txn = [c for c in com_comps if c.name == "exchange_txn"]
        assert all(c.exchange == "mcx" for c in mcx_txn)
        assert all(c.rate == Decimal("0.000021") for c in mcx_txn)

        ctt_comps = [c for c in com_comps if c.name == "ctt"]
        assert all(c.side == "sell" for c in ctt_comps)
        assert all(c.rate == Decimal("0.0001") for c in ctt_comps)


def test_mapping_function_mcx_derivatives_to_com_segment() -> None:
    """Canonical mapping function maps both MCX format variations to segment com."""
    # 1. Canonical colon-separated format
    seg1, prod1, exch1, itype1 = resolve_fee_segment_and_product(
        "MCX:FUT:GOLD:2026-12-04", Product.MIS
    )
    assert seg1 == "com"
    assert prod1 == "MIS"
    assert exch1 == "mcx"
    assert itype1 == "fut"

    # 2. Legacy space-separated format
    seg2, prod2, exch2, itype2 = resolve_fee_segment_and_product(
        "MCX:FNO:GOLD FUT 04 DEC 26", Product.NRML
    )
    assert seg2 == "com"
    assert prod2 == "NRML"
    assert exch2 == "mcx"
    assert itype2 == "fut"


def test_results_writer_removes_unmodelled_caveat_when_mcx_charges_modelled(
    tmp_path: Path,
) -> None:
    """When MCX fills have non-zero modelled charges, 'MCX charges not modelled' is removed."""
    writer = ResultsWriter(run_id="test_mcx_modelled", results_dir=tmp_path / "results")
    trades = [
        {
            "trade_id": "T1",
            "fill_id": "F1",
            "client_ref": "CR1",
            "instrument": "MCX:FUT:GOLD:2026-12-04",
            "side": "BUY",
            "qty": 1,
            "price": 1000000.0,
            "charges": 69.56,
            "ts": "2026-11-13T09:30:00+00:00",
            "ts_ist": "2026-11-13 15:00:00 IST",
        }
    ]
    out_path = writer.write(
        summary_info={
            "run_id": "test_mcx_modelled",
            "mode": "backtest",
            "fee_schedule": "zerodha_nse",
        },
        metrics={"trades": 1, "charges": 69.56},
        trades=trades,
        equity_points=[],
        effective_config_text="",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )
    summary_text = (out_path / "SUMMARY.md").read_text(encoding="utf-8")
    assert "MCX charges not modelled" not in summary_text

    metrics_json = json.loads((out_path / "metrics.json").read_text(encoding="utf-8"))
    assert metrics_json.get("assumptions") != "MCX charges not modelled"


def test_results_writer_retains_unmodelled_caveat_when_mcx_charges_zero(tmp_path: Path) -> None:
    """When MCX fills have charges == 0.0, 'MCX charges not modelled' is retained."""
    writer = ResultsWriter(run_id="test_mcx_zero_charges", results_dir=tmp_path / "results")
    trades = [
        {
            "trade_id": "T1",
            "fill_id": "F1",
            "client_ref": "CR1",
            "instrument": "MCX:FUT:GOLD:2026-12-04",
            "side": "BUY",
            "qty": 1,
            "price": 1000000.0,
            "charges": 0.0,
            "ts": "2026-11-13T09:30:00+00:00",
            "ts_ist": "2026-11-13 15:00:00 IST",
        }
    ]
    out_path = writer.write(
        summary_info={
            "run_id": "test_mcx_zero_charges",
            "mode": "backtest",
            "fee_schedule": "zerodha_nse",
        },
        metrics={"trades": 1, "charges": 0.0},
        trades=trades,
        equity_points=[],
        effective_config_text="",
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )
    summary_text = (out_path / "SUMMARY.md").read_text(encoding="utf-8")
    assert "MCX charges not modelled" in summary_text

    metrics_json = json.loads((out_path / "metrics.json").read_text(encoding="utf-8"))
    assert metrics_json.get("assumptions") == "MCX charges not modelled"


def test_fake_feed_mcx_fill_gives_vector_charges(zerodha_schedule: Schedule) -> None:
    """A test fee run over fills with an MCX fill gives non-zero charges equal to vector sum."""
    fill_ts = datetime(2026, 11, 13, 9, 30, tzinfo=UTC)
    buy_fill = Fill(
        fill_id="F-MCX-1",
        client_ref="CR-1",
        qty=1,
        price=Decimal("1000000.00"),
        ts=fill_ts,
        side=Side.BUY,
        product=Product.MIS,
        instrument="MCX:FUT:GOLD:2026-12-04",
    )
    sell_fill = Fill(
        fill_id="F-MCX-2",
        client_ref="CR-2",
        qty=1,
        price=Decimal("1000000.00"),
        ts=fill_ts,
        side=Side.SELL,
        product=Product.MIS,
        instrument="MCX:FUT:GOLD:2026-12-04",
    )

    results = charges_for_fills([buy_fill, sell_fill], zerodha_schedule)
    assert len(results) == 2
    assert abs(results[0].total - Decimal("69.56")) <= Decimal("0.005")
    assert abs(results[1].total - Decimal("149.56")) <= Decimal("0.005")
    assert abs(results.total - Decimal("219.12")) <= Decimal("0.005")
