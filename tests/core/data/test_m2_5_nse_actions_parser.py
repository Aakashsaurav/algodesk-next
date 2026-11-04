"""Tests for NSE corporate actions parser against trimmed fixtures and synthetic cases."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from plugins.references.nse_actions.parser import parse_nse_item
from plugins.references.nse_actions.plugin import NSEActionsReferencePlugin

from algodesk.api.manifest import PluginManifest

_FIXTURE_DIR = Path(__file__).parents[2] / "fixtures" / "reference" / "nse"


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
    return NSEActionsReferencePlugin(
        manifest=manifest,
        _fixture_path=_FIXTURE_DIR / fixture_name,
        _data_dir=tmp_path / "data",
    )


def test_parser_split_ten_to_two() -> None:
    item = {
        "symbol": "IRCTC",
        "exDate": "28-Oct-2021",
        "subject": "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share",
    }
    recs = parse_nse_item(item)
    assert len(recs) == 1
    assert recs[0].type == "split"
    assert recs[0].ratio == 5.0
    assert recs[0].ratio_old == 10.0
    assert recs[0].ratio_new == 2.0
    assert recs[0].status == "unverified"


def test_parser_split_ten_to_one() -> None:
    item = {
        "symbol": "EICHERMOT",
        "exDate": "24-Aug-2020",
        "subject": "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share",
    }
    recs = parse_nse_item(item)
    assert len(recs) == 1
    assert recs[0].type == "split"
    assert recs[0].ratio == 10.0
    assert recs[0].ratio_old == 10.0
    assert recs[0].ratio_new == 1.0


def test_parser_split_unparseable_text_flags_manual_required() -> None:
    item = {
        "symbol": "TEST",
        "exDate": "01-Jan-2025",
        "subject": "Stock Split of Equity Shares without numbers",
    }
    recs = parse_nse_item(item)
    assert len(recs) == 1
    assert recs[0].type == "split"
    assert recs[0].status == "manual_required"
    assert recs[0].ratio is None


def test_parser_bonus_one_to_one() -> None:
    item = {
        "symbol": "RELIANCE",
        "exDate": "28-Oct-2024",
        "subject": "Bonus 1:1",
    }
    recs = parse_nse_item(item)
    assert len(recs) == 1
    assert recs[0].type == "bonus"
    assert recs[0].ratio == 1.0
    assert recs[0].ratio_new == 1.0
    assert recs[0].ratio_old == 1.0
    assert recs[0].status == "unverified"


def test_parser_bonus_unparseable_flags_manual_required() -> None:
    item = {
        "symbol": "TEST",
        "exDate": "01-Jan-2025",
        "subject": "Bonus Issue of shares announced",
    }
    recs = parse_nse_item(item)
    assert len(recs) == 1
    assert recs[0].type == "bonus"
    assert recs[0].status == "manual_required"


def test_parser_dividend_single_amount() -> None:
    item = {
        "symbol": "RELIANCE",
        "exDate": "19-Aug-2024",
        "subject": "Dividend - Rs 10 Per Share",
    }
    recs = parse_nse_item(item)
    assert len(recs) == 1
    assert recs[0].type == "dividend"
    assert recs[0].amount == Decimal("10")
    assert recs[0].status == "unverified"


def test_parser_dividend_multiple_amounts_summed() -> None:
    item = {
        "symbol": "TCS",
        "exDate": "15-Jan-2024",
        "subject": "Interim Dividend - Rs 9 Per Share Special Dividend - Rs 18 Per Share",
    }
    recs = parse_nse_item(item)
    assert len(recs) == 1
    assert recs[0].type == "dividend"
    assert recs[0].amount == Decimal("27")


def test_parser_dividend_percentage_without_rupee_flags_manual_required() -> None:
    item = {
        "symbol": "TCS",
        "exDate": "06-Jul-2005",
        "subject": "Agm/Dividend-500%",
    }
    recs = parse_nse_item(item)
    assert len(recs) == 1
    assert recs[0].type == "dividend"
    assert recs[0].amount is None
    assert recs[0].status == "manual_required"


def test_parser_rights_with_explicit_price() -> None:
    item = {
        "symbol": "ABC",
        "exDate": "01-Feb-2024",
        "subject": "Rights 1:4 @ Rs 250 Per Share",
    }
    recs = parse_nse_item(item)
    assert len(recs) == 1
    assert recs[0].type == "rights"
    assert recs[0].ratio == 0.25
    assert recs[0].issue_price == Decimal("250")
    assert recs[0].status == "unverified"


def test_parser_rights_with_premium_only_flags_manual_required() -> None:
    item = {
        "symbol": "RELIANCE",
        "exDate": "13-May-2020",
        "subject": "Rights 1:15 @ Premium Rs 1247",
    }
    recs = parse_nse_item(item)
    assert len(recs) == 1
    assert recs[0].type == "rights"
    assert recs[0].ratio == 1.0 / 15.0
    assert recs[0].issue_price is None
    assert recs[0].status == "manual_required"


def test_parser_merger_and_demerger_flags_manual_required() -> None:
    item_demerger = {
        "symbol": "RELIANCE",
        "exDate": "20-Jul-2023",
        "subject": "Demerger",
    }
    recs_demerger = parse_nse_item(item_demerger)
    assert len(recs_demerger) == 1
    assert recs_demerger[0].type == "other"
    assert recs_demerger[0].status == "manual_required"

    item_merger = {
        "symbol": "XYZ",
        "exDate": "10-Oct-2024",
        "subject": "Scheme of Amalgamation / Merger",
    }
    recs_merger = parse_nse_item(item_merger)
    assert len(recs_merger) == 1
    assert recs_merger[0].type == "other"
    assert recs_merger[0].status == "manual_required"


def test_parser_combo_bonus_and_dividend() -> None:
    item = {
        "symbol": "TCS",
        "exDate": "16-Jun-2009",
        "subject": "Bonus 1:1/Div-Rs.5 Per Sh",
    }
    recs = parse_nse_item(item)
    assert len(recs) == 2
    types = {r.type for r in recs}
    assert types == {"bonus", "dividend"}
    b = next(r for r in recs if r.type == "bonus")
    d = next(r for r in recs if r.type == "dividend")
    assert b.ratio == 1.0
    assert d.amount == Decimal("5")


def test_plugin_loads_fixture_and_caches_raw_json(tmp_path: Path) -> None:
    plugin = _make_plugin("reliance_actions.json", tmp_path)
    acts = plugin.corporate_actions("RELIANCE")
    assert len(acts) > 0
    raw_file = list((tmp_path / "data" / "actions" / "raw" / "nse").glob("RELIANCE_*.json"))
    assert len(raw_file) == 1
    data = json.loads(raw_file[0].read_text(encoding="utf-8"))
    assert len(data) == 28
