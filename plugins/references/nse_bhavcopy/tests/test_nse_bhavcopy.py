"""Unit and conformance tests for NSEBhavcopyReferencePlugin without network."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from algodesk.api.conformance import kind_problems
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import SeriesKey

from ..parser import bhavcopy_row_to_bar, parse_bhavcopy_csv
from ..plugin import NSEBhavcopyReferencePlugin

_FIXTURES_DIR = Path(__file__).parents[4] / "tests" / "fixtures" / "reference" / "nse_bhavcopy"
_IST = ZoneInfo("Asia/Kolkata")


def _make_manifest() -> PluginManifest:
    return PluginManifest.model_validate(
        {
            "name": "nse_bhavcopy",
            "kind": "reference",
            "version": "0.1.0",
            "api_version": 1,
            "entry": "plugin:NSEBhavcopyReferencePlugin",
            "verified_on": "2026-11-04",
            "sources": [
                "https://nsearchives.nseindia.com/content/cm/",
                "https://nsearchives.nseindia.com/content/historical/EQUITIES/",
            ],
            "capabilities": {"bhavcopy": True, "daily_bars": True},
            "limits": {
                "corporate_actions": {
                    "requests_per_second": 1,
                    "requests_per_minute": 30,
                    "requests_per_30min": 600,
                },
                "bhavcopy": {
                    "requests_per_second": 1,
                    "requests_per_minute": 30,
                    "requests_per_30min": 600,
                },
            },
        }
    )


def test_conformance(tmp_path: Path) -> None:
    manifest = _make_manifest()
    plugin = NSEBhavcopyReferencePlugin(
        manifest=manifest,
        _fixture_path=_FIXTURES_DIR,
        _data_dir=tmp_path / "data",
    )
    problems = kind_problems(plugin)
    assert not problems, f"Conformance problems: {problems}"


def test_parser_udiff_format() -> None:
    csv_text = (_FIXTURES_DIR / "20241028.csv").read_text(encoding="utf-8")
    rows = parse_bhavcopy_csv(csv_text, target_symbols=["RELIANCE"])
    assert len(rows) == 1
    rel = rows[0]
    assert rel.symbol == "RELIANCE"
    assert rel.series == "EQ"
    assert rel.isin == "INE002A01018"
    assert rel.trade_date == date(2024, 10, 28)
    assert rel.close == Decimal("1334.35")
    assert rel.prev_close == Decimal("2655.70")


def test_parser_legacy_format() -> None:
    csv_text = (_FIXTURES_DIR / "20220728.csv").read_text(encoding="utf-8")
    rows = parse_bhavcopy_csv(csv_text, target_symbols=["TATASTEEL"])
    assert len(rows) == 1
    ts = rows[0]
    assert ts.symbol == "TATASTEEL"
    assert ts.series == "EQ"
    assert ts.trade_date == date(2022, 7, 28)
    assert ts.close == Decimal("100.35")
    assert ts.prev_close == Decimal("959.40")


def test_parser_isin_filtering() -> None:
    csv_text = (_FIXTURES_DIR / "20241028.csv").read_text(encoding="utf-8")
    rows = parse_bhavcopy_csv(csv_text, target_isins=["INE002A01018"])
    assert len(rows) == 1
    assert rows[0].symbol == "RELIANCE"


def test_bhavcopy_row_to_bar() -> None:
    csv_text = (_FIXTURES_DIR / "20241028.csv").read_text(encoding="utf-8")
    rows = parse_bhavcopy_csv(csv_text, target_symbols=["RELIANCE"])
    bar = bhavcopy_row_to_bar(rows[0], _IST)
    assert str(bar.instrument) == "NSE:EQ:RELIANCE"
    assert bar.timeframe == "1d"
    assert bar.close == 1334.35
    assert bar.complete is True


def test_daily_bars_from_fixtures(tmp_path: Path) -> None:
    manifest = _make_manifest()
    plugin = NSEBhavcopyReferencePlugin(
        manifest=manifest,
        _fixture_path=_FIXTURES_DIR,
        _data_dir=tmp_path / "data",
    )
    bars = plugin.daily_bars(["RELIANCE"], date(2024, 10, 25), date(2024, 10, 28))
    # 25th (Fri) and 28th (Mon) have fixtures
    dates = {b.start_ts.astimezone(_IST).date() for b in bars}
    assert date(2024, 10, 25) in dates
    assert date(2024, 10, 28) in dates


def test_history_wrapper(tmp_path: Path) -> None:
    manifest = _make_manifest()
    plugin = NSEBhavcopyReferencePlugin(
        manifest=manifest,
        _fixture_path=_FIXTURES_DIR,
        _data_dir=tmp_path / "data",
    )
    key = SeriesKey(
        instrument="NSE:EQ:RELIANCE",  # type: ignore[arg-type]
        kind="bars",
        timeframe="1d",
        adjustment="raw",
        source="nse_bhavcopy",
    )
    st = datetime(2024, 10, 25, 0, 0, tzinfo=UTC)
    en = datetime(2024, 10, 28, 23, 59, tzinfo=UTC)
    bars = list(plugin.history(key, st, en))
    assert len(bars) >= 2

