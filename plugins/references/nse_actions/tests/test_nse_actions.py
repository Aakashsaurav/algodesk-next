"""Unit and conformance tests for NSEActionsReferencePlugin without network."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from algodesk.api.conformance import kind_problems, reference_problems
from algodesk.api.manifest import PluginManifest

from ..parser import parse_nse_item
from ..plugin import NSEActionsReferencePlugin

_FIXTURE_DIR = Path(__file__).parents[4] / "tests" / "fixtures" / "reference" / "nse"


def _make_plugin(fixture_name: str, tmp_path: Path) -> NSEActionsReferencePlugin:
    manifest = PluginManifest.model_validate(
        {
            "name": "nse_actions",
            "kind": "reference",
            "version": "0.1.0",
            "api_version": 1,
            "entry": "plugin:NSEActionsReferencePlugin",
            "verified_on": "2026-11-03",
            "sources": ["https://www.nseindia.com"],
            "capabilities": {"corporate_actions": True},
            "limits": {
                "corporate_actions": {
                    "requests_per_second": 1,
                    "requests_per_minute": 30,
                    "requests_per_30min": 600,
                }
            },
        }
    )
    fixture_path = _FIXTURE_DIR / fixture_name
    return NSEActionsReferencePlugin(
        manifest=manifest,
        _fixture_path=fixture_path,
        _data_dir=tmp_path / "data",
    )


def test_conformance_suite(tmp_path: Path) -> None:
    plugin = _make_plugin("reliance_actions.json", tmp_path)
    problems = kind_problems(plugin)
    assert not problems, f"Conformance problems: {problems}"
    ref_probs = reference_problems(plugin)
    assert not ref_probs, f"Reference problems: {ref_probs}"


def test_reliance_bonus_and_rights_and_raw_cache(tmp_path: Path) -> None:
    plugin = _make_plugin("reliance_actions.json", tmp_path)
    actions = plugin.corporate_actions("NSE:EQ:RELIANCE")
    assert len(actions) > 0

    # Check bonus 2024-10-28
    bonuses = [a for a in actions if a.type == "bonus" and a.ex_date == date(2024, 10, 28)]
    assert len(bonuses) == 1
    assert bonuses[0].ratio == 1.0
    assert bonuses[0].ratio_new == 1.0
    assert bonuses[0].ratio_old == 1.0
    assert bonuses[0].status == "unverified"

    # Check rights 2020-05-13 (ratio 1:15, premium Rs 1247 -> manual_required)
    rights = [a for a in actions if a.type == "rights" and a.ex_date == date(2020, 5, 13)]
    assert len(rights) == 1
    assert rights[0].ratio == 1.0 / 15.0
    assert rights[0].status == "manual_required"

    # Check demerger 2023-07-20
    demergers = [a for a in actions if a.ex_date == date(2023, 7, 20)]
    assert len(demergers) == 1
    assert demergers[0].type == "other"
    assert demergers[0].status == "manual_required"

    # Check raw cache file written under data/actions/raw/nse/
    raw_files = list((tmp_path / "data" / "actions" / "raw" / "nse").glob("RELIANCE_*.json"))
    assert len(raw_files) == 1
    cached_data = json.loads(raw_files[0].read_text(encoding="utf-8"))
    assert len(cached_data) > 0


def test_irctc_stock_split(tmp_path: Path) -> None:
    plugin = _make_plugin("irctc_actions.json", tmp_path)
    actions = plugin.corporate_actions("IRCTC")
    splits = [a for a in actions if a.type == "split" and a.ex_date == date(2021, 10, 28)]
    assert len(splits) == 1
    assert splits[0].ratio_old == 10.0
    assert splits[0].ratio_new == 2.0
    assert splits[0].ratio == 5.0
    assert splits[0].status == "unverified"


def test_eichermot_stock_split_ten_to_one(tmp_path: Path) -> None:
    plugin = _make_plugin("eichermot_actions.json", tmp_path)
    actions = plugin.corporate_actions("EICHERMOT")
    splits = [a for a in actions if a.type == "split" and a.ex_date == date(2020, 8, 24)]
    assert len(splits) == 1
    assert splits[0].ratio_old == 10.0
    assert splits[0].ratio_new == 1.0
    assert splits[0].ratio == 10.0


def test_tcs_dividends_and_date_filtering(tmp_path: Path) -> None:
    plugin = _make_plugin("tcs_actions.json", tmp_path)
    filtered = plugin.corporate_actions(
        "TCS",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )
    assert len(filtered) > 0
    for a in filtered:
        assert date(2024, 1, 1) <= a.ex_date <= date(2024, 12, 31)


def test_parser_synthetic_cases() -> None:
    # Explicit rights price
    rec = parse_nse_item(
        {
            "symbol": "ABC",
            "exDate": "15-Jan-2025",
            "subject": "Rights 1:2 @ Rs 50 Per Share",
        }
    )
    assert len(rec) == 1
    assert rec[0].type == "rights"
    assert rec[0].ratio == 0.5
    assert rec[0].issue_price == Decimal("50")
    assert rec[0].status == "unverified"

    # Combined bonus + dividend
    rec_combo = parse_nse_item(
        {
            "symbol": "XYZ",
            "exDate": "20-May-2025",
            "subject": "Bonus 1:1 / Dividend - Rs 5 Per Share",
        }
    )
    assert len(rec_combo) == 2
    b = next(a for a in rec_combo if a.type == "bonus")
    d = next(a for a in rec_combo if a.type == "dividend")
    assert b.ratio == 1.0
    assert d.amount == Decimal("5")

    # Merger text
    rec_merger = parse_nse_item(
        {
            "symbol": "MNO",
            "exDate": "10-Jun-2025",
            "subject": "Scheme of Amalgamation and Merger",
        }
    )
    assert len(rec_merger) == 1
    assert rec_merger[0].type == "other"
    assert rec_merger[0].status == "manual_required"
