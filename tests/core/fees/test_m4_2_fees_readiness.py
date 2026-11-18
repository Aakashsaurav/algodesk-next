"""Tests for CP2 Step 8 & 9: Fee selection by trade date, fail closed, fallback, and CLI show."""

from __future__ import annotations

import json
import tomllib
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from algodesk.api.market import InstrumentId
from algodesk.api.trading import Fill, Product, Side
from algodesk.cli.app import app
from algodesk.fees import FeeEngine, FeeScheduleError, Schedule, charges_for_fills

runner = CliRunner()


@pytest.fixture
def zerodha_schedule() -> Schedule:
    schedule_path = (
        Path(__file__).resolve().parents[3] / "plugins" / "fees" / "zerodha_nse" / "schedule.toml"
    )
    with open(schedule_path, "rb") as f:
        data = tomllib.load(f)
    return Schedule.model_validate(data)


@pytest.fixture(autouse=True)
def reset_engine_state() -> None:
    FeeEngine.reset_fallback_count()
    FeeEngine.set_fallback(None)
    yield
    FeeEngine.reset_fallback_count()
    FeeEngine.set_fallback(None)


def test_fee_selection_before_earliest_fails_closed(zerodha_schedule: Schedule) -> None:
    """Trade dated before earliest verified effective_from raises FeeScheduleError."""
    # 2019-12-31 is before earliest verified date (2020-01-01) for eq:CNC components
    with pytest.raises(FeeScheduleError) as exc_info:
        FeeEngine.calculate_trade_charges(
            turnover=Decimal("10000.00"),
            side=Side.BUY,
            segment="eq",
            product=Product.CNC,
            trade_date=date(2019, 12, 31),
            schedule=zerodha_schedule,
            fallback=None,
        )

    err = exc_info.value
    assert "before earliest verified" in err.what
    assert "FeeEngine:eq:CNC" in err.where
    assert "fees.fallback = 'earliest'" in err.fix


def test_fee_schedule_error_attributes() -> None:
    """FeeScheduleError has standard AlgoDeskError fields."""
    err = FeeScheduleError(
        what="No verified rate",
        where="FeeEngine.test",
        fix="Set fees.fallback = 'earliest'",
        trade_date=date(2019, 12, 31),
        component="brokerage",
        earliest_verified=date(2020, 1, 1),
    )
    assert err.trade_date == date(2019, 12, 31)
    assert err.component == "brokerage"
    assert err.earliest_verified == date(2020, 1, 1)
    assert "No verified rate" in str(err)


def test_fee_selection_with_fallback_earliest_succeeds(zerodha_schedule: Schedule) -> None:
    """Option fees.fallback = 'earliest' uses earliest rate for pre-verified dates."""
    charges = FeeEngine.calculate_trade_charges(
        turnover=Decimal("10000.00"),
        side=Side.BUY,
        segment="eq",
        product=Product.CNC,
        trade_date=date(2019, 12, 31),
        schedule=zerodha_schedule,
        fallback="earliest",
    )

    assert charges.total > Decimal("0")
    assert FeeEngine.get_fallback_used() == 1


def test_fee_fallback_used_count_accumulates(zerodha_schedule: Schedule) -> None:
    """FeeEngine tracks how many calculations used earliest fallback."""
    FeeEngine.calculate_trade_charges(
        turnover=Decimal("10000.00"),
        side=Side.BUY,
        segment="eq",
        product=Product.CNC,
        trade_date=date(2019, 12, 30),
        schedule=zerodha_schedule,
        fallback="earliest",
    )
    FeeEngine.calculate_trade_charges(
        turnover=Decimal("10000.00"),
        side=Side.SELL,
        segment="eq",
        product=Product.CNC,
        trade_date=date(2019, 12, 31),
        schedule=zerodha_schedule,
        fallback="earliest",
    )
    # Trade on verified date (2026-11-13) does not use fallback
    FeeEngine.calculate_trade_charges(
        turnover=Decimal("10000.00"),
        side=Side.BUY,
        segment="eq",
        product=Product.CNC,
        trade_date=date(2026, 11, 13),
        schedule=zerodha_schedule,
        fallback="earliest",
    )

    assert FeeEngine.get_fallback_used() == 2


def test_fee_selection_on_effective_date_picks_correct_version(zerodha_schedule: Schedule) -> None:
    """FeeEngine picks the verified component effective as-of trade date without fallback."""
    FeeEngine.set_fallback(None)
    charges = FeeEngine.calculate_trade_charges(
        turnover=Decimal("10000.00"),
        side=Side.BUY,
        segment="eq",
        product=Product.CNC,
        trade_date=date(2026, 11, 13),
        schedule=zerodha_schedule,
        fallback=None,
    )
    assert charges.total > Decimal("0")
    assert FeeEngine.get_fallback_used() == 0


def test_charges_for_fills_propagates_fallback(zerodha_schedule: Schedule) -> None:
    """charges_for_fills uses configured fallback and tracks fallback count."""
    fill = Fill(
        fill_id="f1",
        client_ref="c1",
        qty=10,
        price=Decimal("2500.00"),
        ts=datetime(2019, 12, 31, 9, 30, tzinfo=UTC),
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        side=Side.BUY,
        product=Product.CNC,
    )
    charges_list = charges_for_fills(
        fills=[fill],
        schedule=zerodha_schedule,
        fallback="earliest",
    )
    assert len(charges_list) == 1
    assert charges_list[0].total > Decimal("0")
    assert FeeEngine.get_fallback_used() == 1


def test_charges_for_fills_fail_closed_without_fallback(zerodha_schedule: Schedule) -> None:
    """charges_for_fills fails closed when fill date precedes verified range."""
    fill = Fill(
        fill_id="f2",
        client_ref="c2",
        qty=10,
        price=Decimal("2500.00"),
        ts=datetime(2019, 12, 31, 9, 30, tzinfo=UTC),
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        side=Side.BUY,
        product=Product.CNC,
    )
    with pytest.raises(FeeScheduleError):
        charges_for_fills(
            fills=[fill],
            schedule=zerodha_schedule,
            fallback=None,
        )


def test_cli_fees_show_valid_date(tmp_project: Path) -> None:
    """CLI fees show prints components and rates for verified date."""
    res = runner.invoke(
        app, ["--root", str(tmp_project), "fees", "show", "zerodha_nse", "--as-of", "2026-11-13"]
    )
    assert res.exit_code == 0
    assert "zerodha_nse" in res.stdout
    assert "brokerage" in res.stdout


def test_cli_fees_show_out_of_bounds_date_fails(tmp_project: Path) -> None:
    """CLI fees show prints actionable FeeScheduleError for date prior to verified range."""
    res = runner.invoke(
        app, ["--root", str(tmp_project), "fees", "show", "zerodha_nse", "--as-of", "2022-01-03"]
    )
    assert res.exit_code == 1
    assert "FeeScheduleError" in res.stdout or "ERROR:" in res.stdout
    assert "zerodha_nse" in res.stdout
    assert "earliest" in res.stdout or "effective_from" in res.stdout


def test_cli_fees_show_json_format(tmp_project: Path) -> None:
    """CLI fees show --json produces valid JSON with rates and components."""
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "fees",
            "show",
            "zerodha_nse",
            "--as-of",
            "2026-11-13",
            "--json",
        ],
    )
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert data["schedule"] == "zerodha_nse"
    assert data["as_of"] == "2026-11-13"
    assert "components" in data
    assert len(data["components"]) > 0


def test_cli_fees_show_nonexistent_schedule(tmp_project: Path) -> None:
    """CLI fees show for nonexistent schedule exits with error."""
    res = runner.invoke(app, ["--root", str(tmp_project), "fees", "show", "nonexistent_xyz"])
    assert res.exit_code != 0
    assert "not found" in res.stdout.lower() or "error" in res.stdout.lower()
