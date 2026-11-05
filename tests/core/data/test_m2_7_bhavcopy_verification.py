"""Unit and integration tests for M2-7: NSE Bhavcopy ingestion, trading-day signals,
and price-based corporate action verification.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from plugins.references.nse_bhavcopy.parser import (
    bhavcopy_row_to_bar,
    parse_bhavcopy_csv,
)
from plugins.references.nse_bhavcopy.plugin import NSEBhavcopyReferencePlugin
from typer.testing import CliRunner

from algodesk.api.manifest import PluginManifest
from algodesk.api.market import ActionRecord, Bar, InstrumentId, SeriesKey
from algodesk.cli import app
from algodesk.data.actions_store import ActionsStore
from algodesk.data.adjust import AdjustmentView
from algodesk.data.closures import infer_market_closures
from algodesk.data.coverage import CoverageIndex
from algodesk.data.detection import (
    _calc_expected_factor,
    verify_prices_for_actions,
)
from algodesk.data.manifest import DataManifest
from algodesk.data.planner import GapPlanner
from algodesk.data.store import BarStore

_FIXTURES_DIR = Path(__file__).parents[3] / "tests" / "fixtures" / "reference" / "nse_bhavcopy"
_IST = ZoneInfo("Asia/Kolkata")


def _make_bhavcopy_manifest() -> PluginManifest:
    return PluginManifest.model_validate(
        {
            "name": "nse_bhavcopy",
            "kind": "reference",
            "version": "0.1.0",
            "api_version": 1,
            "entry": "plugin:NSEBhavcopyReferencePlugin",
            "verified_on": "2026-11-04",
            "sources": ["https://nsearchives.nseindia.com"],
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


# --- Step 1: URL patterns, dates, columns, sizes, holiday/weekend response ---


def test_step1_udiff_format_columns_and_dates() -> None:
    csv_text = (_FIXTURES_DIR / "20241028.csv").read_text(encoding="utf-8")
    rows = parse_bhavcopy_csv(csv_text)
    assert len(rows) >= 1
    row = rows[0]
    assert row.symbol == "RELIANCE"
    assert row.trade_date == date(2024, 10, 28)
    assert isinstance(row.open, Decimal)
    assert isinstance(row.high, Decimal)
    assert isinstance(row.low, Decimal)
    assert isinstance(row.close, Decimal)
    assert isinstance(row.prev_close, Decimal)
    assert isinstance(row.volume, int)
    assert row.isin.startswith("IN")


def test_step1_legacy_format_columns_and_dates() -> None:
    csv_text = (_FIXTURES_DIR / "20220728.csv").read_text(encoding="utf-8")
    rows = parse_bhavcopy_csv(csv_text, target_symbols=["TATASTEEL"])
    assert len(rows) == 1
    row = rows[0]
    assert row.symbol == "TATASTEEL"
    assert row.trade_date == date(2022, 7, 28)
    assert isinstance(row.close, Decimal)
    assert isinstance(row.prev_close, Decimal)
    assert row.isin.startswith("IN")


def test_step1_bhavcopy_url_pattern_selection() -> None:
    plugin = NSEBhavcopyReferencePlugin(manifest=_make_bhavcopy_manifest())
    u_new = plugin._bhavcopy_url(date(2024, 10, 28))
    assert "BhavCopy_NSE_CM_0_0_0_20241028_F_0000.csv.zip" in u_new
    u_old = plugin._bhavcopy_url(date(2022, 7, 28))
    assert "cm28JUL2022bhav.csv.zip" in u_old


# --- Step 2: Raw check for RELIANCE 1:1 bonus & TATASTEEL 10:1 split ---


def test_step2_raw_check_reliance_bonus() -> None:
    row_prev = parse_bhavcopy_csv(
        (_FIXTURES_DIR / "20241025.csv").read_text(encoding="utf-8"),
        target_symbols=["RELIANCE"],
    )[0]
    row_ex = parse_bhavcopy_csv(
        (_FIXTURES_DIR / "20241028.csv").read_text(encoding="utf-8"),
        target_symbols=["RELIANCE"],
    )[0]
    ratio = float(row_ex.close / row_prev.close)
    # Observed ratio is approximately 0.5024 (halved, bonus 1:1)
    assert abs(ratio - 0.50) < 0.02


def test_step2_raw_check_tatasteel_split() -> None:
    row_prev = parse_bhavcopy_csv(
        (_FIXTURES_DIR / "20220727.csv").read_text(encoding="utf-8"),
        target_symbols=["TATASTEEL"],
    )[0]
    row_ex = parse_bhavcopy_csv(
        (_FIXTURES_DIR / "20220728.csv").read_text(encoding="utf-8"),
        target_symbols=["TATASTEEL"],
    )[0]
    ratio = float(row_ex.close / row_prev.close)
    # Observed ratio is approximately 0.1046 (tenth, split 10:1)
    assert abs(ratio - 0.10) < 0.02


# --- Step 3: Terms and robots verification ---


def test_step3_robots_txt_documented_in_spike() -> None:
    spike_path = Path(__file__).parents[3] / "_workbench" / "spike_bhavcopy.md"
    assert spike_path.is_file()
    content = spike_path.read_text(encoding="utf-8")
    assert "robots.txt" in content.lower()
    assert "Disallow: /market-data-test" in content


# --- Step 4: daily_bars, universe filtering, storage with basis=none ---


def test_step4_universe_filtering_by_symbol() -> None:
    csv_text = (_FIXTURES_DIR / "20241028.csv").read_text(encoding="utf-8")
    rows = parse_bhavcopy_csv(csv_text, target_symbols=["TCS"])
    assert len(rows) == 1
    assert rows[0].symbol == "TCS"


def test_step4_universe_filtering_by_isin() -> None:
    csv_text = (_FIXTURES_DIR / "20241028.csv").read_text(encoding="utf-8")
    rows = parse_bhavcopy_csv(csv_text, target_isins=["INE467B01029"])  # TCS ISIN
    assert len(rows) == 1
    assert rows[0].symbol == "TCS"


def test_step4_daily_bars_storage_and_metadata(tmp_path: Path) -> None:
    manifest = DataManifest(tmp_path / "manifest.db")
    store = BarStore(tmp_path / "data", manifest, exchange_tz=_IST)
    plugin = NSEBhavcopyReferencePlugin(
        manifest=_make_bhavcopy_manifest(),
        _fixture_path=_FIXTURES_DIR,
        _data_dir=tmp_path / "data",
        _store=store,
        _manifest=manifest,
    )
    bars = plugin.daily_bars(["RELIANCE"], date(2024, 10, 25), date(2024, 10, 28))
    assert len(bars) >= 2

    # Verify dataset record was stored with adjustment_basis="none"
    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1d",
        adjustment="raw",
        source="nse_bhavcopy",
    )
    ds_id = store.dataset_id(key)
    ds_rec = manifest.get_dataset(ds_id)
    assert ds_rec is not None
    assert ds_rec.adjustment_basis == "none"
    assert ds_rec.adjustment == "raw"


def test_step4_daily_bars_isin_resolution(tmp_path: Path) -> None:
    manifest = DataManifest(tmp_path / "manifest.db")
    store = BarStore(tmp_path / "data", manifest, exchange_tz=_IST)
    plugin = NSEBhavcopyReferencePlugin(
        manifest=_make_bhavcopy_manifest(),
        _fixture_path=_FIXTURES_DIR,
        _data_dir=tmp_path / "data",
        _store=store,
        _manifest=manifest,
    )
    # Pass ISIN directly
    bars = plugin.daily_bars(["INE002A01018"], date(2024, 10, 25), date(2024, 10, 28))
    assert len(bars) >= 2
    assert all(str(b.instrument) == "NSE:EQ:RELIANCE" for b in bars)


# --- Step 5: Trading-day signal: exchange_file closure & special weekend ---


def test_step5_special_session_from_weekend_file(tmp_path: Path) -> None:
    manifest = DataManifest(tmp_path / "manifest.db")
    plugin = NSEBhavcopyReferencePlugin(
        manifest=_make_bhavcopy_manifest(),
        _fixture_path=_FIXTURES_DIR,
        _data_dir=tmp_path / "data",
        _manifest=manifest,
    )
    # Saturday 2025-02-01 Union Budget special session
    plugin.daily_bars(["RELIANCE"], date(2025, 2, 1), date(2025, 2, 1))
    closure = manifest.get_market_closure("NSE", "2025-02-01")
    assert closure is not None
    assert closure.kind == "special"


def test_step5_exchange_file_closure_on_absent_weekday(tmp_path: Path) -> None:
    manifest = DataManifest(tmp_path / "manifest.db")
    plugin = NSEBhavcopyReferencePlugin(
        manifest=_make_bhavcopy_manifest(),
        _fixture_path=_FIXTURES_DIR,  # Does not have 2024-05-01 (holiday)
        _data_dir=tmp_path / "data",
        _manifest=manifest,
    )
    # 2024-05-01 was Wednesday May Day
    plugin.daily_bars(["RELIANCE"], date(2024, 5, 1), date(2024, 5, 1))
    closure = manifest.get_market_closure("NSE", "2024-05-01")
    assert closure is not None
    assert closure.kind == "exchange_file"


def test_step5_exchange_file_closure_outranks_inferred(tmp_path: Path) -> None:
    manifest = DataManifest(tmp_path / "manifest.db")
    # Pre-record an official exchange_file closure
    manifest.record_market_closure("NSE", "2024-05-01", kind="exchange_file", evidence="test")

    mock_ds = MagicMock()
    mock_ds._exchange_tz = UTC
    mock_ds._default_source = "yfinance"
    # Even if infer_market_closures runs, exchange_file closure is not overwritten
    infer_market_closures(
        manifest=manifest,
        data_service=mock_ds,
        exchange="NSE",
        start_d=date(2024, 5, 1),
        end_d=date(2024, 5, 1),
        sample_symbols=["NSE:EQ:RELIANCE"],
    )
    final_rec = manifest.get_market_closure("NSE", "2024-05-01")
    assert final_rec is not None
    assert final_rec.kind == "exchange_file"


def test_step5_planner_respects_exchange_file_closure(tmp_path: Path) -> None:
    manifest = DataManifest(tmp_path / "manifest.db")
    manifest.record_market_closure("NSE", "2024-05-01", kind="exchange_file", evidence="official")
    planner = GapPlanner()
    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1d",
        adjustment="raw",
        source="nse_bhavcopy",
    )
    st = datetime(2024, 5, 1, 9, 15, tzinfo=_IST).astimezone(UTC)
    en = datetime(2024, 5, 1, 15, 30, tzinfo=_IST).astimezone(UTC)
    plan = planner.plan(
        key,
        st,
        en,
        manifest=manifest,
        calendar=None,
    )
    # Official closure suppresses chunk generation
    assert date(2024, 5, 1) in plan.closed_calendar_days
    assert len(plan.chunks) == 0


def test_step5_coverage_index_respects_exchange_file_closure(tmp_path: Path) -> None:
    manifest = DataManifest(tmp_path / "manifest.db")
    manifest.record_market_closure("NSE", "2024-05-01", kind="exchange_file", evidence="official")
    cov = CoverageIndex(manifest)
    ds_id = "bars:nse_bhavcopy:1d:NSE:EQ:RELIANCE:raw"
    gaps = cov.detect_gaps(ds_id, date(2024, 5, 1), date(2024, 5, 1))
    # Not reported as missing gap because official closure applies
    assert len(gaps) == 0


# --- Step 6: data get with basis=none converting to splits/total ---


def test_step6_adjustment_view_from_basis_none_to_splits() -> None:
    d_prev = datetime(2024, 10, 25, 9, 15, tzinfo=UTC)
    d_ex = datetime(2024, 10, 28, 9, 15, tzinfo=UTC)
    inst = InstrumentId("NSE:EQ:RELIANCE")
    b_prev = Bar(inst, "1d", d_prev, d_prev, 2687.0, 2688.7, 2644.0, 2655.70, 1000, True, False)
    b_ex = Bar(inst, "1d", d_ex, d_ex, 1337.0, 1353.0, 1322.1, 1334.35, 2000, True, False)

    action = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0,
        ratio_new=1.0,
        ratio_old=1.0,
        status="confirmed",
        source="nse",
        symbol="RELIANCE",
    )
    res = AdjustmentView.apply(
        [b_prev, b_ex],
        [action],
        from_basis="none",
        to_basis="splits",
        exchange_tz=_IST,
    )
    assert len(res.bars) == 2
    # Pre-bonus bar price is halved to 1327.85
    assert abs(res.bars[0].close - (2655.70 / 2.0)) < 1e-2
    # Ex-date bar price remains unadjusted (1334.35)
    assert abs(res.bars[1].close - 1334.35) < 1e-2


def test_step6_adjustment_view_from_basis_none_to_none_is_identity() -> None:
    d = datetime(2024, 10, 28, 9, 15, tzinfo=UTC)
    b = Bar(
        InstrumentId("NSE:EQ:RELIANCE"),
        "1d",
        d,
        d,
        1337.0,
        1353.0,
        1322.1,
        1334.35,
        1000,
        True,
        False,
    )
    res = AdjustmentView.apply([b], [], from_basis="none", to_basis="none", exchange_tz=_IST)
    assert res.bars[0].close == 1334.35


# --- Step 7: Price-based verification of splits and bonuses ---


def test_step7_calc_expected_factor_split() -> None:
    # 10:1 split (old=10, new=1) -> expected price factor 0.10
    act = ActionRecord(
        date(2022, 7, 28), "split", ratio_old=10.0, ratio_new=1.0, symbol="TATASTEEL"
    )
    assert abs(_calc_expected_factor(act) - 0.10) < 1e-4


def test_step7_calc_expected_factor_bonus() -> None:
    # 1:1 bonus (new=1, old=1) -> expected price factor 0.50
    act = ActionRecord(date(2024, 10, 28), "bonus", ratio_new=1.0, ratio_old=1.0, symbol="RELIANCE")
    assert abs(_calc_expected_factor(act) - 0.50) < 1e-4


def test_step7_price_verification_confirmed_match() -> None:
    inst = InstrumentId("NSE:EQ:RELIANCE")
    d_prev = datetime(2024, 10, 25, 9, 15, tzinfo=UTC)
    d_ex = datetime(2024, 10, 28, 9, 15, tzinfo=UTC)
    b_prev = Bar(inst, "1d", d_prev, d_prev, 2687.0, 2688.7, 2644.0, 2655.70, 1000, True, False)
    b_ex = Bar(inst, "1d", d_ex, d_ex, 1337.0, 1353.0, 1322.1, 1334.35, 2000, True, False)

    act = ActionRecord(date(2024, 10, 28), "bonus", ratio_new=1.0, ratio_old=1.0, symbol="RELIANCE")
    results = verify_prices_for_actions("RELIANCE", [b_prev, b_ex], [act], tolerance_pct=5.0)
    assert len(results) == 1
    res = results[0]
    assert res.verdict == "confirmed"
    assert abs(res.declared_factor - 0.50) < 1e-3
    assert abs(res.observed_factor - (1334.35 / 2655.70)) < 1e-3


def test_step7_price_verification_conflict_mismatch() -> None:
    inst = InstrumentId("NSE:EQ:XYZ")
    d_prev = datetime(2024, 10, 25, 9, 15, tzinfo=UTC)
    d_ex = datetime(2024, 10, 28, 9, 15, tzinfo=UTC)
    # Price did NOT drop; remained 1000.0
    b_prev = Bar(inst, "1d", d_prev, d_prev, 1000.0, 1000.0, 1000.0, 1000.0, 100, True, False)
    b_ex = Bar(inst, "1d", d_ex, d_ex, 1000.0, 1000.0, 1000.0, 1000.0, 100, True, False)

    act = ActionRecord(date(2024, 10, 28), "split", ratio_old=10.0, ratio_new=1.0, symbol="XYZ")
    results = verify_prices_for_actions("XYZ", [b_prev, b_ex], [act], tolerance_pct=5.0)
    assert len(results) == 1
    assert results[0].verdict == "conflict"


def test_step7_price_verification_suspect_event_unexplained_jump() -> None:
    inst = InstrumentId("NSE:EQ:ABC")
    d_1 = datetime(2024, 10, 25, 9, 15, tzinfo=UTC)
    d_2 = datetime(2024, 10, 28, 9, 15, tzinfo=UTC)
    # 50% price crash without declared action
    b_1 = Bar(inst, "1d", d_1, d_1, 1000.0, 1000.0, 1000.0, 1000.0, 100, True, False)
    b_2 = Bar(inst, "1d", d_2, d_2, 500.0, 500.0, 500.0, 500.0, 100, True, False)

    results = verify_prices_for_actions("ABC", [b_1, b_2], [], jump_pct=20.0)
    assert len(results) == 1
    assert results[0].verdict == "suspect_event"
    assert results[0].event_type == "jump"


# --- Step 8: CLI verify-prices command ---


def test_step8_cli_verify_prices_help() -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["data", "actions", "verify-prices", "--help"])
    assert res.exit_code == 0
    assert "--from" in res.output
    assert "--to" in res.output
    assert "--source" in res.output


def test_step8_cli_verify_prices_execution(tmp_project: Path) -> None:
    store = ActionsStore(data_dir=tmp_project / "data")
    manifest = DataManifest(tmp_project / "data" / "manifest.db")
    bar_store = BarStore(tmp_project / "data", manifest, exchange_tz=_IST)

    # Ingest Reliance bonus action
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

    # Ingest Reliance bars into store
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
    assert "BONUS" in res.output
    assert "confirmed" in res.output
    assert "0.5000" in res.output


def test_step8_cli_verify_prices_no_actions(tmp_project: Path) -> None:
    runner = CliRunner()
    cmd = [
        "data",
        "actions",
        "verify-prices",
        "TCS",
        "--from",
        "2024-10-25",
        "--to",
        "2024-10-28",
    ]
    res = runner.invoke(app, cmd)
    assert res.exit_code == 0
    assert "No corporate actions or price anomalies found" in res.output


def test_step8_cli_verify_prices_conflict_output(tmp_project: Path) -> None:
    store = ActionsStore(data_dir=tmp_project / "data")
    manifest = DataManifest(tmp_project / "data" / "manifest.db")
    bar_store = BarStore(tmp_project / "data", manifest, exchange_tz=_IST)

    # Write split action that doesn't match flat prices
    act = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="split",
        ratio=10.0,
        ratio_old=10.0,
        ratio_new=1.0,
        source="nse",
        symbol="FLATSYM",
    )
    store.write("NSE", "FLATSYM", [act])

    d_1 = datetime(2024, 10, 25, 9, 15, tzinfo=UTC)
    d_2 = datetime(2024, 10, 28, 9, 15, tzinfo=UTC)
    inst = InstrumentId("NSE:EQ:FLATSYM")
    b_1 = Bar(inst, "1d", d_1, d_1, 500.0, 500.0, 500.0, 500.0, 100, True, False)
    b_2 = Bar(inst, "1d", d_2, d_2, 500.0, 500.0, 500.0, 500.0, 100, True, False)
    key = SeriesKey(inst, "bars", "1d", "raw", "nse_bhavcopy")
    bar_store.commit(key, [b_1, b_2])

    runner = CliRunner()
    cmd = [
        "data",
        "actions",
        "verify-prices",
        "FLATSYM",
        "--from",
        "2024-10-25",
        "--to",
        "2024-10-28",
        "--source",
        "nse_bhavcopy",
    ]
    res = runner.invoke(app, cmd)
    assert res.exit_code == 0
    assert "FLATSYM" in res.output
    assert "conflict" in res.output
