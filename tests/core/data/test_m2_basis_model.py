"""Tests for M2-4b CP0 adjustment basis model and evidence-shaped fixture."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from algodesk.api.errors import DataError
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import ActionRecord, Bar, InstrumentId, SeriesKey
from algodesk.data.adjust import AdjustmentView
from algodesk.data.manifest import DataManifest, DatasetRecord
from algodesk.data.service import DataService
from algodesk.data.store import BarStore

_TZ = ZoneInfo("Asia/Kolkata")


def _make_bar(
    symbol: str,
    d: date,
    close: float,
    timeframe: str = "1d",
    hour: int = 9,
    minute: int = 15,
    vol: int = 1000,
) -> Bar:
    st = datetime(d.year, d.month, d.day, hour, minute, tzinfo=_TZ).astimezone(UTC)
    delta = (
        timedelta(days=1)
        if timeframe == "1d"
        else timedelta(hours=1 if timeframe == "1h" else 0, minutes=1 if timeframe == "1m" else 0)
    )
    return Bar(
        instrument=InstrumentId(f"NSE:EQ:{symbol}"),
        timeframe=timeframe,
        start_ts=st,
        end_ts=st + delta,
        open=close - 1.0,
        high=close + 2.0,
        low=close - 2.0,
        close=close,
        volume=vol,
        complete=True,
    )


def _build_evidence_fixture() -> tuple[list[Bar], list[Bar], list[ActionRecord]]:
    """Fixture shaped like evidence (RELIANCE: bonus on 2024-10-28, dividends on ex-dates).

    yfinance Close shows no break across 2024-10-28 bonus (already split/bonus adjusted).
    Adj Close differs only by dividend steps.
    """
    actions = [
        ActionRecord(
            ex_date=date(2024, 8, 19),
            type="dividend",
            amount=Decimal("10.0"),
            source="upstox",
            symbol="RELIANCE",
            exchange="NSE",
            status="confirmed",
        ),
        ActionRecord(
            ex_date=date(2024, 10, 28),
            type="bonus",
            ratio=2.0,
            ratio_new=1.0,
            ratio_old=1.0,
            source="upstox",
            symbol="RELIANCE",
            exchange="NSE",
            status="confirmed",
        ),
        ActionRecord(
            ex_date=date(2025, 8, 14),
            type="dividend",
            amount=Decimal("10.0"),
            source="upstox",
            symbol="RELIANCE",
            exchange="NSE",
            status="confirmed",
        ),
        ActionRecord(
            ex_date=date(2026, 6, 5),
            type="dividend",
            amount=Decimal("10.0"),
            source="upstox",
            symbol="RELIANCE",
            exchange="NSE",
            status="confirmed",
        ),
    ]

    dates = [
        date(2024, 8, 16),
        date(2024, 8, 19),  # div 1
        date(2024, 10, 25),
        date(2024, 10, 28),  # bonus 1:1 (Close has NO break!)
        date(2025, 8, 13),
        date(2025, 8, 14),  # div 2
        date(2026, 6, 4),
        date(2026, 6, 5),  # div 3
        date(2026, 10, 30),
    ]

    # Stored close series (Close has no step at 2024-10-28 bonus)
    close_prices = [
        1350.0,
        1355.0,
        1360.0,
        1340.0,  # 2024-10-28 bonus ex-date: continuous ~1340-1360 range, NO step down!
        1400.0,
        1405.0,
        1450.0,
        1455.0,
        1480.0,
    ]

    close_bars = [_make_bar("RELIANCE", d, p) for d, p in zip(dates, close_prices, strict=True)]

    # Compute expected Adj Close (differs ONLY by dividends)
    # Dividend factors:
    # 2026-06-05: f3 = 1 - 10 / 1450 = 1440 / 1450 = 0.9931034
    # 2025-08-14: f2 = 1 - 10 / 1400 = 1390 / 1400 = 0.9928571
    # 2024-08-19: f1 = 1 - 10 / 1350 = 1340 / 1350 = 0.9925926
    f1 = 1.0 - (10.0 / 1350.0)
    f2 = 1.0 - (10.0 / 1400.0)
    f3 = 1.0 - (10.0 / 1450.0)

    adj_close_bars: list[Bar] = []
    for b in close_bars:
        d = b.start_ts.astimezone(_TZ).date()
        f = 1.0
        if d < date(2024, 8, 19):
            f *= f1
        if d < date(2025, 8, 14):
            f *= f2
        if d < date(2026, 6, 5):
            f *= f3
        adj_close_bars.append(_make_bar("RELIANCE", d, b.close * f, vol=b.volume))

    return close_bars, adj_close_bars, actions


def test_adjust_splits_returns_series_unchanged() -> None:
    """adjust=splits on splits-basis series returns the series unchanged."""
    close_bars, _, actions = _build_evidence_fixture()
    result = AdjustmentView.apply(
        bars=close_bars,
        actions=actions,
        from_basis="splits",
        to_basis="splits",
        exchange_tz=_TZ,
    )
    assert len(result.bars) == len(close_bars)
    for orig, adj in zip(close_bars, result.bars, strict=True):
        assert orig.close == adj.close
        assert orig.volume == adj.volume
    assert result.action_ids == []
    assert result.factor_kinds == []


def test_adjust_total_matches_fixture_adj_close_within_tolerance() -> None:
    """adjust=total on splits-basis series matches fixture Adj Close within audit tolerance."""
    close_bars, adj_bars, actions = _build_evidence_fixture()
    result = AdjustmentView.apply(
        bars=close_bars,
        actions=actions,
        from_basis="splits",
        to_basis="total",
        exchange_tz=_TZ,
    )
    assert len(result.bars) == len(adj_bars)
    for expected, actual in zip(adj_bars, result.bars, strict=True):
        assert pytest.approx(actual.close, rel=0.0005) == expected.close
    assert result.factor_kinds == ["dividend"]
    assert len(result.action_ids) == 3  # exactly the 3 dividends


def test_adjust_none_then_splits_round_trips() -> None:
    """adjust=none then splits round-trips cleanly, preserving all price and volume data."""
    close_bars, _, actions = _build_evidence_fixture()

    # Step 1: reverse splits and bonuses to get truly raw unadjusted series
    to_none = AdjustmentView.apply(
        bars=close_bars,
        actions=actions,
        from_basis="splits",
        to_basis="none",
        exchange_tz=_TZ,
    )
    # Prior to 2024-10-28 bonus, price should be multiplied by 2.0 (1350 -> 2700)
    assert pytest.approx(to_none.bars[0].close, rel=1e-4) == 2700.0
    assert pytest.approx(to_none.bars[2].close, rel=1e-4) == 2720.0
    # On and after bonus date, price remains unchanged
    assert pytest.approx(to_none.bars[3].close, rel=1e-4) == 1340.0

    # Step 2: forward adjust from none to splits
    to_splits = AdjustmentView.apply(
        bars=to_none.bars,
        actions=actions,
        from_basis="none",
        to_basis="splits",
        exchange_tz=_TZ,
    )
    assert len(to_splits.bars) == len(close_bars)
    for orig, round_tripped in zip(close_bars, to_splits.bars, strict=True):
        assert pytest.approx(round_tripped.close, rel=1e-4) == orig.close
        assert round_tripped.volume == orig.volume


def test_property_no_event_is_applied_twice() -> None:
    """A split/bonus event is never applied twice when going from splits to total."""
    close_bars, _, actions = _build_evidence_fixture()
    res1 = AdjustmentView.apply(
        bars=close_bars,
        actions=actions,
        from_basis="splits",
        to_basis="total",
        exchange_tz=_TZ,
    )
    # 2024-10-25 price should only be modified by dividends, NEVER by bonus again
    # If bonus were applied twice, it would drop from ~1360.0 to ~680.0
    assert res1.bars[2].close > 1300.0
    assert "bonus" not in res1.factor_kinds
    assert "split" not in res1.factor_kinds


def test_views_agree_across_timeframes() -> None:
    """Views agree across 1m, 1h, and 1d timeframes with identical daily factors."""
    d = date(2024, 8, 16)
    action = ActionRecord(
        ex_date=date(2024, 8, 19),
        type="dividend",
        amount=Decimal("10.0"),
        source="upstox",
        symbol="RELIANCE",
        exchange="NSE",
        status="confirmed",
    )
    b_1d = _make_bar("RELIANCE", d, 1350.0, timeframe="1d")
    b_1h = _make_bar("RELIANCE", d, 1350.0, timeframe="1h", hour=10)
    b_1m = _make_bar("RELIANCE", d, 1350.0, timeframe="1m", hour=10, minute=30)

    daily_ref = [b_1d]
    res_1d = AdjustmentView.apply(
        [b_1d],
        [action],
        from_basis="splits",
        to_basis="total",
        exchange_tz=_TZ,
        daily_bars=daily_ref,
    )
    res_1h = AdjustmentView.apply(
        [b_1h],
        [action],
        from_basis="splits",
        to_basis="total",
        exchange_tz=_TZ,
        daily_bars=daily_ref,
    )
    res_1m = AdjustmentView.apply(
        [b_1m],
        [action],
        from_basis="splits",
        to_basis="total",
        exchange_tz=_TZ,
        daily_bars=daily_ref,
    )

    assert pytest.approx(res_1d.bars[0].close, rel=1e-5) == res_1h.bars[0].close
    assert pytest.approx(res_1h.bars[0].close, rel=1e-5) == res_1m.bars[0].close


def test_total_to_splits_or_none_refuses_with_clear_error() -> None:
    """Converting from total adjusted series refuses with clear error."""
    b = _make_bar("RELIANCE", date(2026, 10, 1), 1000.0)
    with pytest.raises(DataError) as exc_splits:
        AdjustmentView.apply([b], [], from_basis="total", to_basis="splits")
    assert "Cannot convert 'total' adjusted series to 'splits' basis" in str(exc_splits.value)

    with pytest.raises(DataError) as exc_none:
        AdjustmentView.apply([b], [], from_basis="total", to_basis="none")
    assert "Cannot convert 'total' adjusted series to 'none' basis" in str(exc_none.value)


def test_splits_to_none_refuses_if_any_split_bonus_unverified() -> None:
    """splits -> none refuses if any split or bonus is unverified, conflict or manual_required."""
    b = _make_bar("RELIANCE", date(2026, 10, 1), 1000.0)
    unv_action = ActionRecord(
        ex_date=date(2026, 10, 2),
        type="split",
        ratio=2.0,
        source="upstox",
        symbol="RELIANCE",
        exchange="NSE",
        status="unverified",
    )
    with pytest.raises(DataError) as exc:
        AdjustmentView.apply(
            [b], [unv_action], from_basis="splits", to_basis="none", exchange_tz=_TZ
        )
    err = str(exc.value)
    assert "Cannot reverse splits" in err
    assert "unverified" in err


def test_manifest_dataset_record_adjustment_basis(tmp_path: Path) -> None:
    """DataManifest migrates and stores adjustment_basis per dataset."""
    db_path = tmp_path / "manifest.db"
    m = DataManifest(db_path)
    rec_splits = DatasetRecord(
        dataset_id="bars:upstox:1d:NSE:EQ:RELIANCE:adjusted",
        instrument="NSE:EQ:RELIANCE",
        kind="bars",
        timeframe="1d",
        adjustment="adjusted",
        source="upstox",
        adjustment_basis="splits",
    )
    m.record_dataset(rec_splits)

    loaded = m.get_dataset(rec_splits.dataset_id)
    assert loaded is not None
    assert loaded.adjustment_basis == "splits"


def test_provenance_records_basis_and_factor_kinds(tmp_path: Path) -> None:
    """DataService.get provenance records basis, from_basis, factor_kinds, action_ids, hash."""
    from algodesk.data.actions_store import ActionsStore

    db_path = tmp_path / "manifest.db"
    m = DataManifest(db_path)
    store = BarStore(tmp_path, manifest=m)
    acts_store = ActionsStore(tmp_path, manifest=m)

    key = SeriesKey(InstrumentId("NSE:EQ:RELIANCE"), "bars", "1d", "raw", "upstox")
    b1 = _make_bar("RELIANCE", date(2026, 10, 1), 1000.0)
    b2 = _make_bar("RELIANCE", date(2026, 10, 2), 1000.0)
    store.commit(key, [b1, b2])

    div_act = ActionRecord(
        ex_date=date(2026, 10, 2),
        type="dividend",
        amount=Decimal("10.0"),
        source="upstox",
        symbol="RELIANCE",
        exchange="NSE",
        status="confirmed",
    )
    acts_store.write("NSE", "RELIANCE", [div_act])

    u_manifest = PluginManifest.model_validate(
        {
            "name": "upstox",
            "kind": "broker",
            "version": "0.1.0",
            "api_version": 1,
            "entry": "pkg:Plugin",
            "capabilities": {"history": True, "corporate_actions": True},
            "candles_adjustment_basis": "splits",
        }
    )

    svc = DataService(
        sources={},
        store=store,
        manifests={"upstox": u_manifest},
    )

    slice_res = svc.get(
        series=key,
        start=datetime(2026, 10, 1, tzinfo=UTC),
        end=datetime(2026, 10, 3, tzinfo=UTC),
        download="raw",
        adjust="total",
        accept_unverified=True,
    )
    prov = slice_res.provenance
    assert prov["basis"] == "total"
    assert prov["from_basis"] == "splits"
    assert prov["factor_kinds"] == ["dividend"]
    assert len(prov["action_ids"]) == 1
    assert len(prov["actions_hash"]) == 64
