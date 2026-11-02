"""Comprehensive coverage tests for adjustment basis conversions, reconciler, and CLI formatting."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from algodesk.api import ActionRecord, Bar, InstrumentId, PluginManifest
from algodesk.api.errors import DataError
from algodesk.cli.data import _format_action_row, _format_summary_counts
from algodesk.cli.spike import _determine_basis_match
from algodesk.data.adjust import AdjustmentView
from algodesk.data.manifest import DataManifest, DatasetRecord
from algodesk.data.reconciler import CorporateActionsReconciler

_TZ = ZoneInfo("Asia/Kolkata")


def _make_bar(
    symbol: str,
    d: date,
    close: float,
    timeframe: str = "1d",
    vol: int = 1000,
) -> Bar:
    st = datetime(d.year, d.month, d.day, 9, 15, tzinfo=_TZ).astimezone(UTC)
    return Bar(
        instrument=InstrumentId(f"NSE:EQ:{symbol}"),
        timeframe=timeframe,
        start_ts=st,
        end_ts=st + timedelta(days=1),
        open=close,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=vol,
        complete=True,
    )


# ---------------------------------------------------------------------------
# Basis Conversion Permutations (none, splits, total)
# ---------------------------------------------------------------------------


def test_basis_none_to_splits_applies_split_factors() -> None:
    # 2:1 split on 2024-10-28: prices before should divide by 2, volume multiply by 2
    b1 = _make_bar("TEST", date(2024, 10, 25), close=100.0, vol=100)
    b2 = _make_bar("TEST", date(2024, 10, 28), close=50.0, vol=200)
    act = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="split",
        ratio=2.0,
        source="upstox",
        symbol="TEST",
        exchange="NSE",
        status="confirmed",
    )

    view = AdjustmentView()
    res = view.apply([b1, b2], [act], to_basis="splits", from_basis="none")
    bars, action_ids, _ = res

    assert len(bars) == 2
    assert bars[0].close == pytest.approx(50.0)
    assert bars[0].volume == 50
    assert bars[1].close == pytest.approx(50.0)
    assert bars[1].volume == 200
    assert "split" in res.factor_kinds
    assert res.basis == "splits"


def test_basis_none_to_total_applies_splits_and_dividends() -> None:
    b1 = _make_bar("TEST", date(2024, 8, 10), close=100.0)
    b2 = _make_bar("TEST", date(2024, 8, 15), close=90.0)  # after dividend 10.0
    b3 = _make_bar("TEST", date(2024, 10, 28), close=45.0)  # after 2:1 split
    acts = [
        ActionRecord(
            ex_date=date(2024, 8, 14),
            type="dividend",
            amount=Decimal("10.0"),
            source="upstox",
            symbol="TEST",
            exchange="NSE",
            status="confirmed",
        ),
        ActionRecord(
            ex_date=date(2024, 10, 28),
            type="split",
            ratio=2.0,
            source="upstox",
            symbol="TEST",
            exchange="NSE",
            status="confirmed",
        ),
    ]

    view = AdjustmentView()
    res = view.apply([b1, b2, b3], acts, to_basis="total", from_basis="none")
    bars = res.bars
    # b1 has both dividend factor (1 - 10/100 = 0.9) and split factor (0.5)
    # -> 100 * 0.9 * 0.5 = 45.0
    assert bars[0].close == pytest.approx(45.0)
    # b2 is after dividend, so only split factor (0.5) -> 90 * 0.5 = 45.0
    assert bars[1].close == pytest.approx(45.0)
    assert bars[2].close == pytest.approx(45.0)
    assert set(res.factor_kinds) == {"dividend", "split"}


def test_basis_none_to_none_returns_unchanged() -> None:
    b1 = _make_bar("TEST", date(2024, 10, 25), close=100.0)
    act = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="split",
        ratio=2.0,
        source="upstox",
        symbol="TEST",
        status="confirmed",
    )
    res = AdjustmentView().apply([b1], [act], to_basis="none", from_basis="none")
    assert res.bars[0].close == 100.0
    assert len(res.factor_kinds) == 0


def test_basis_total_to_total_returns_unchanged() -> None:
    b1 = _make_bar("TEST", date(2024, 10, 25), close=45.0)
    act = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="split",
        ratio=2.0,
        source="yfinance",
        symbol="TEST",
        status="confirmed",
    )
    res = AdjustmentView().apply([b1], [act], to_basis="total", from_basis="total")
    assert res.bars[0].close == 45.0
    assert len(res.factor_kinds) == 0


def test_basis_total_to_splits_raises_data_error() -> None:
    b1 = _make_bar("TEST", date(2024, 10, 25), close=45.0)
    with pytest.raises(DataError) as exc:
        AdjustmentView().apply([b1], [], to_basis="splits", from_basis="total")
    assert "Cannot convert 'total' adjusted series to 'splits' basis" in exc.value.what


def test_basis_total_to_none_raises_data_error() -> None:
    b1 = _make_bar("TEST", date(2024, 10, 25), close=45.0)
    with pytest.raises(DataError) as exc:
        AdjustmentView().apply([b1], [], to_basis="none", from_basis="total")
    assert "Cannot convert 'total' adjusted series to 'none' basis" in exc.value.what


def test_basis_splits_to_none_volume_adjustment() -> None:
    # splits basis means b1 volume was already multiplied by 2.
    # Reversing split divides volume by 1/r (multiplies by r)!
    b1 = _make_bar("TEST", date(2024, 10, 25), close=50.0, vol=2000)
    b2 = _make_bar("TEST", date(2024, 10, 28), close=50.0, vol=2000)
    act = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=2.0,
        source="upstox",
        symbol="TEST",
        status="confirmed",
    )
    res = AdjustmentView().apply([b1, b2], [act], to_basis="none", from_basis="splits")
    assert res.bars[0].close == pytest.approx(100.0)
    assert res.bars[0].volume == 4000
    assert res.bars[1].close == pytest.approx(50.0)
    assert res.bars[1].volume == 2000


def test_basis_splits_to_none_refuses_on_unverified_split() -> None:
    b1 = _make_bar("TEST", date(2024, 10, 25), close=50.0)
    act = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="split",
        ratio=2.0,
        source="upstox",
        symbol="TEST",
        status="unverified",
    )
    with pytest.raises(DataError) as exc:
        AdjustmentView().apply([b1], [act], to_basis="none", from_basis="splits")
    assert "Cannot reverse splits" in exc.value.what
    assert "unverified" in exc.value.what


def test_basis_splits_to_none_refuses_on_conflict_split() -> None:
    b1 = _make_bar("TEST", date(2024, 10, 25), close=50.0)
    act = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="split",
        ratio=2.0,
        source="upstox",
        symbol="TEST",
        status="conflict",
    )
    with pytest.raises(DataError) as exc:
        AdjustmentView().apply([b1], [act], to_basis="none", from_basis="splits")
    assert "conflict" in exc.value.what


def test_basis_splits_to_none_refuses_on_manual_required_bonus() -> None:
    b1 = _make_bar("TEST", date(2024, 10, 25), close=50.0)
    act = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=None,
        source="upstox",
        symbol="TEST",
        status="manual_required",
    )
    with pytest.raises(DataError) as exc:
        AdjustmentView().apply([b1], [act], to_basis="none", from_basis="splits")
    assert "manual_required" in exc.value.what


def test_basis_splits_to_none_allows_unverified_dividend() -> None:
    # Reversing splits/bonuses only requires split/bonus to be confirmed.
    # An unverified dividend is irrelevant to split reversal.
    b1 = _make_bar("TEST", date(2024, 10, 25), close=50.0)
    act_div = ActionRecord(
        ex_date=date(2024, 8, 14),
        type="dividend",
        amount=Decimal("5.0"),
        source="yfinance",
        symbol="TEST",
        status="unverified",
    )
    act_split = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="split",
        ratio=2.0,
        source="upstox",
        symbol="TEST",
        status="confirmed",
    )
    res = AdjustmentView().apply([b1], [act_div, act_split], to_basis="none", from_basis="splits")
    assert res.bars[0].close == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Reconciler Permutations and Edge Cases
# ---------------------------------------------------------------------------


def test_reconciler_prefers_primary_source_when_both_have_ratios() -> None:
    a1 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=2.0,
        source="upstox",
        symbol="TEST",
        exchange="NSE",
        text="Upstox official",
    )
    a2 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=2.0,
        source="yfinance",
        symbol="TEST",
        exchange="NSE",
        text="Yahoo finance official",
    )
    rec = CorporateActionsReconciler(primary_source="upstox").reconcile("NSE", "TEST", [a1, a2])
    assert len(rec) == 1
    assert rec[0].source == "upstox"
    assert rec[0].text == "Upstox official"
    assert rec[0].status == "confirmed"


def test_reconciler_detects_ex_date_disagreement() -> None:
    # Same event type across two sources with 1 day difference
    a1 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=2.0,
        source="upstox",
        symbol="TEST",
        exchange="NSE",
    )
    a2 = ActionRecord(
        ex_date=date(2024, 10, 29),
        type="bonus",
        ratio=2.0,
        source="yfinance",
        symbol="TEST",
        exchange="NSE",
    )
    rec = CorporateActionsReconciler().reconcile("NSE", "TEST", [a1, a2])
    assert all(a.status == "conflict" for a in rec)
    assert any("ex_date disagreement" in (a.reason or "") for a in rec)


def test_reconciler_cash_dividend_within_tolerance_confirmed() -> None:
    a1 = ActionRecord(
        ex_date=date(2024, 8, 14),
        type="dividend",
        amount=Decimal("10.00"),
        source="upstox",
        symbol="TEST",
        exchange="NSE",
    )
    a2 = ActionRecord(
        ex_date=date(2024, 8, 14),
        type="dividend",
        amount=Decimal("10.05"),  # 0.5% diff < 1.0% tolerance
        source="yfinance",
        symbol="TEST",
        exchange="NSE",
    )
    rec = CorporateActionsReconciler(dividend_tolerance_pct=1.0).reconcile("NSE", "TEST", [a1, a2])
    assert len(rec) == 1
    assert rec[0].status == "confirmed"


def test_reconciler_cash_dividend_exceeding_tolerance_conflict() -> None:
    a1 = ActionRecord(
        ex_date=date(2024, 8, 14),
        type="dividend",
        amount=Decimal("10.00"),
        source="upstox",
        symbol="TEST",
        exchange="NSE",
    )
    a2 = ActionRecord(
        ex_date=date(2024, 8, 14),
        type="dividend",
        amount=Decimal("12.00"),  # 20% diff > 1.0% tolerance
        source="yfinance",
        symbol="TEST",
        exchange="NSE",
    )
    rec = CorporateActionsReconciler(dividend_tolerance_pct=1.0).reconcile("NSE", "TEST", [a1, a2])
    assert len(rec) == 1
    assert rec[0].status == "conflict"


def test_reconciler_type_mismatch_flags_conflict() -> None:
    a1 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="split",
        ratio=2.0,
        source="upstox",
        symbol="TEST",
        exchange="NSE",
    )
    a2 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=2.0,
        source="yfinance",
        symbol="TEST",
        exchange="NSE",
    )
    rec = CorporateActionsReconciler().reconcile("NSE", "TEST", [a1, a2])
    assert len(rec) == 1
    assert rec[0].status == "conflict"
    assert "type mismatch" in (rec[0].reason or "")


# ---------------------------------------------------------------------------
# DataManifest Basis Column & Plugin Manifest Helper
# ---------------------------------------------------------------------------


def test_manifest_basis_column_schema_and_migration(tmp_path) -> None:
    db_path = tmp_path / "data_manifest.db"
    manifest = DataManifest(db_path)

    # Record dataset with default splits basis
    ds = DatasetRecord(
        dataset_id="bars:upstox:1d:NSE:EQ:RELIANCE:raw",
        instrument="NSE:EQ:RELIANCE",
        kind="bars",
        timeframe="1d",
        adjustment="raw",
        source="upstox",
        schema_version=1,
        created_at="2026-11-02T10:00:00Z",
        adjustment_basis="splits",
    )
    manifest.record_dataset(ds)

    read_ds = manifest.get_dataset("bars:upstox:1d:NSE:EQ:RELIANCE:raw")
    assert read_ds is not None
    assert read_ds.adjustment_basis == "splits"

    all_ds = manifest.list_datasets()
    assert len(all_ds) == 1
    assert all_ds[0].adjustment_basis == "splits"


def test_plugin_manifest_get_candles_adjustment_basis() -> None:
    # String config (upstox)
    m_upstox = PluginManifest(
        name="upstox",
        kind="broker",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="plugin:UpstoxBrokerPlugin",
        capabilities={"history": True},
        candles_adjustment_basis="splits",
    )
    assert m_upstox.get_candles_adjustment_basis("raw") == "splits"
    assert m_upstox.get_candles_adjustment_basis("adjusted") == "splits"

    # Dict config (yfinance)
    m_yf = PluginManifest(
        name="yfinance",
        kind="broker",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="plugin:YFinanceBrokerPlugin",
        capabilities={"history": True},
        candles_adjustment_basis={"raw": "splits", "adjusted": "total"},
    )
    assert m_yf.get_candles_adjustment_basis("raw") == "splits"
    assert m_yf.get_candles_adjustment_basis("adjusted") == "total"

    # None fallback
    m_none = PluginManifest(
        name="none",
        kind="broker",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="plugin:NonePlugin",
        capabilities={"history": True},
    )
    assert m_none.get_candles_adjustment_basis("raw") == "splits"
    assert m_none.get_candles_adjustment_basis("adjusted") == "splits"


# ---------------------------------------------------------------------------
# CLI formatting and spike basis match helpers
# ---------------------------------------------------------------------------


def test_format_action_row_edge_cases() -> None:
    # Ratio integer vs float
    r_int = _format_action_row(
        "TEST", "2024-10-28", "split", 2.0, None, "upstox", "confirmed", "OK"
    )
    assert r_int[3] == "2"

    r_float = _format_action_row(
        "TEST", "2024-10-28", "split", 1.5, None, "upstox", "confirmed", "OK"
    )
    assert r_float[3] == "1.5"

    # Amount Decimal
    r_amt = _format_action_row(
        "TEST", "2024-08-14", "dividend", None, Decimal("10.5"), "upstox", "confirmed", "OK"
    )
    assert r_amt[3] == "10.5"

    # Neither
    r_none = _format_action_row("TEST", "2024-08-14", "other", None, None, "", "unverified", "")
    assert r_none[3] == "-"
    assert r_none[4] == "-"
    assert r_none[6] == "-"


def test_format_summary_counts_empty_and_multiple() -> None:
    assert _format_summary_counts([]) == ""
    assert (
        _format_summary_counts(["confirmed", "unverified", "confirmed"])
        == "confirmed=2, unverified=1"
    )


def test_compare_adjustment_spike_basis_determination() -> None:
    # Case: Upstox matches yfinance Close at ex-dates (raw_ratio ~ 1.0, adj_ratio != 1.0)
    steps_splits = [
        {"raw_ratio": 1.0001, "adj_ratio": 0.9950},
        {"raw_ratio": 0.9999, "adj_ratio": 0.9910},
    ]
    basis, verdict = _determine_basis_match([1.0, 1.0], [0.99, 0.99], steps_splits)
    assert basis == "splits"
    assert "splits basis" in verdict

    # Case: Upstox matches yfinance Adj Close
    steps_total = [
        {"raw_ratio": 1.0500, "adj_ratio": 1.0001},
        {"raw_ratio": 1.0800, "adj_ratio": 0.9999},
    ]
    basis_t, verdict_t = _determine_basis_match([1.05, 1.08], [1.0, 1.0], steps_total)
    assert basis_t == "total"
    assert "total basis" in verdict_t

    # Case: Neither matches
    steps_unknown = [
        {"raw_ratio": 1.2500, "adj_ratio": 1.2000},
    ]
    basis_u, verdict_u = _determine_basis_match([1.25], [1.20], steps_unknown)
    assert basis_u == "unknown"
    assert "neither = unknown" in verdict_u


def test_actions_list_raw_bypasses_reconciler(tmp_project) -> None:
    from typer.testing import CliRunner

    from algodesk.cli import app
    from algodesk.data.actions_store import ActionsStore

    data_dir = tmp_project / "data"
    store = ActionsStore(data_dir=data_dir)
    # 2 conflicting bonus actions on same date
    a1 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0,
        source="upstox",
        symbol="RELIANCE",
        exchange="NSE",
        status="unverified",
    )
    a2 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=2.0,
        source="yfinance",
        symbol="RELIANCE",
        exchange="NSE",
        status="unverified",
    )
    store.write("NSE", "RELIANCE", [a1, a2])

    runner = CliRunner()
    # Without --raw, reconciler collapses them to conflict
    res_rec = runner.invoke(
        app, ["--root", str(tmp_project), "data", "actions", "list", "RELIANCE"]
    )
    assert res_rec.exit_code == 0
    assert "conflict" in res_rec.stdout

    # With --raw, both unverified records appear directly without reconciliation
    res_raw = runner.invoke(
        app, ["--root", str(tmp_project), "data", "actions", "list", "--raw", "RELIANCE"]
    )
    assert res_raw.exit_code == 0
    assert "conflict" not in res_raw.stdout
    assert "Summary counts: unverified=2" in res_raw.stdout


def test_data_verify_with_adjustment_basis(tmp_project) -> None:
    from typer.testing import CliRunner

    from algodesk.cli import app

    runner = CliRunner()
    res = runner.invoke(app, ["--root", str(tmp_project), "data", "verify"])
    assert res.exit_code == 0
    assert "VERIFY: OK" in res.stdout
