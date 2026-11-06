"""Tests for M2-8 CP1 Step 3: Price verification on basis-none closes against standard ratios."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from plugins.references.nse_bhavcopy.parser import bhavcopy_row_to_bar, parse_bhavcopy_csv
from typer.testing import CliRunner

from algodesk.api.market import ActionRecord, Bar, InstrumentId, SeriesKey
from algodesk.cli import app
from algodesk.data.actions_store import ActionsStore
from algodesk.data.detection import STANDARD_RATIOS, verify_prices_for_actions
from algodesk.data.manifest import DataManifest
from algodesk.data.store import BarStore

_FIXTURES_DIR = Path(__file__).parents[3] / "tests" / "fixtures" / "reference" / "nse_bhavcopy"
_IST = ZoneInfo("Asia/Kolkata")


def test_verify_prices_reliance_20241028_fixture_confirms_at_05024(tmp_project: Path) -> None:
    """The real RELIANCE 2024-10-28 bhavcopy fixture must confirm at factor ~0.5024."""
    csv_prev = (_FIXTURES_DIR / "20241025.csv").read_text(encoding="utf-8")
    csv_ex = (_FIXTURES_DIR / "20241028.csv").read_text(encoding="utf-8")

    bar_prev = bhavcopy_row_to_bar(parse_bhavcopy_csv(csv_prev, ["RELIANCE"])[0], _IST)
    bar_ex = bhavcopy_row_to_bar(parse_bhavcopy_csv(csv_ex, ["RELIANCE"])[0], _IST)

    # Raw closes: 2655.70 and 1334.35 -> 1334.35 / 2655.70 = 0.502447...
    assert bar_prev.close == 2655.70
    assert bar_ex.close == 1334.35
    obs_factor = bar_ex.close / bar_prev.close
    assert pytest.approx(obs_factor, abs=0.0005) == 0.5024

    act = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0,
        ratio_new=1.0,
        ratio_old=1.0,
        source="nse",
        symbol="RELIANCE",
    )

    results = verify_prices_for_actions(
        symbol="RELIANCE",
        bars=[bar_prev, bar_ex],
        actions=[act],
        tolerance_pct=8.0,
    )
    assert len(results) == 1
    r = results[0]
    assert r.verdict == "confirmed"
    assert pytest.approx(r.observed_factor, abs=0.0005) == 0.5024
    assert r.declared_factor == 0.5000


def test_verify_prices_tatasteel_20220728_split_fixture_confirms(tmp_project: Path) -> None:
    """The real TATASTEEL 2022-07-28 split fixture (10:1 split) must confirm."""
    csv_prev = (_FIXTURES_DIR / "20220727.csv").read_text(encoding="utf-8")
    csv_ex = (_FIXTURES_DIR / "20220728.csv").read_text(encoding="utf-8")

    bar_prev = bhavcopy_row_to_bar(parse_bhavcopy_csv(csv_prev, ["TATASTEEL"])[0], _IST)
    bar_ex = bhavcopy_row_to_bar(parse_bhavcopy_csv(csv_ex, ["TATASTEEL"])[0], _IST)

    # 10:1 split: face value split from 10 to 1
    act = ActionRecord(
        ex_date=date(2022, 7, 28),
        type="split",
        ratio=10.0,
        ratio_old=10.0,
        ratio_new=1.0,
        source="nse",
        symbol="TATASTEEL",
    )

    results = verify_prices_for_actions(
        symbol="TATASTEEL",
        bars=[bar_prev, bar_ex],
        actions=[act],
        tolerance_pct=8.0,
    )
    assert len(results) == 1
    r = results[0]
    assert r.verdict == "confirmed"
    assert r.declared_factor == 0.1000
    assert pytest.approx(r.observed_factor, abs=0.01) == 0.1045


def test_standard_ratios_coverage() -> None:
    """Standard ratio set includes common Indian market split and bonus ratios."""
    assert 0.5 in STANDARD_RATIOS  # 1:1 bonus or 2:1 split
    assert 0.1 in STANDARD_RATIOS  # 10:1 split
    assert 0.2 in STANDARD_RATIOS  # 5:1 split or 4:1 bonus
    assert 0.25 in STANDARD_RATIOS  # 4:1 split or 3:1 bonus
    assert pytest.approx(1.0 / 3.0, abs=1e-4) in STANDARD_RATIOS  # 3:1 split or 2:1 bonus


def test_verify_prices_conflict_on_abnormal_move() -> None:
    """When observed move deviates significantly (>8%) from standard ratio, mark conflict."""
    d1 = datetime(2024, 10, 25, 9, 15, tzinfo=UTC)
    d2 = datetime(2024, 10, 28, 9, 15, tzinfo=UTC)
    b1 = Bar(
        InstrumentId("NSE:EQ:TEST"), "1d", d1, d1, 1000.0, 1000.0, 1000.0, 1000.0, 100, True, False
    )
    # 1000 -> 800 is a 20% drop, declared factor is 0.5 (1:1 bonus)
    b2 = Bar(
        InstrumentId("NSE:EQ:TEST"), "1d", d2, d2, 800.0, 800.0, 800.0, 800.0, 100, True, False
    )

    act = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0,
        ratio_new=1.0,
        ratio_old=1.0,
        source="nse",
        symbol="TEST",
    )

    results = verify_prices_for_actions(
        symbol="TEST",
        bars=[b1, b2],
        actions=[act],
        tolerance_pct=8.0,
    )
    assert len(results) == 1
    assert results[0].verdict == "conflict"


def test_cli_actions_verify_prices_with_fixture(tmp_project: Path) -> None:
    """CLI verify-prices command produces tabular confirmed output with 8% tolerance."""
    manifest = DataManifest(tmp_project / "data" / "manifest.db")
    store = ActionsStore(data_dir=tmp_project / "data", manifest=manifest)
    bar_store = BarStore(tmp_project / "data", manifest, exchange_tz=_IST)

    act = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0,
        ratio_new=1.0,
        ratio_old=1.0,
        source="nse",
        symbol="RELIANCE",
    )
    store.write("NSE", "RELIANCE", [act])

    csv_prev = (_FIXTURES_DIR / "20241025.csv").read_text(encoding="utf-8")
    csv_ex = (_FIXTURES_DIR / "20241028.csv").read_text(encoding="utf-8")
    b_prev = bhavcopy_row_to_bar(parse_bhavcopy_csv(csv_prev, ["RELIANCE"])[0], _IST)
    b_ex = bhavcopy_row_to_bar(parse_bhavcopy_csv(csv_ex, ["RELIANCE"])[0], _IST)
    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1d",
        adjustment="raw",
        source="nse_bhavcopy",
    )
    bar_store.commit(key, [b_prev, b_ex])

    runner = CliRunner()
    cmd = [
        "data",
        "actions",
        "verify-prices",
        "RELIANCE",
        "--from",
        "2024-10-25",
        "--to",
        "2024-10-28",
        "--source",
        "nse_bhavcopy",
    ]
    res = runner.invoke(app, cmd)
    assert res.exit_code == 0
    assert "RELIANCE" in res.output
    assert "confirmed" in res.output
    assert "0.5024" in res.output
    assert "0.5000" in res.output
