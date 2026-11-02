"""Unit and matrix tests for M2-4 CP2 adjusted views, AdjustmentView, and staleness."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from algodesk.api import (
    ActionRecord,
    Bar,
    DataError,
    InstrumentId,
    PluginManifest,
    SeriesKey,
)
from algodesk.data.adjust import AdjustmentView
from algodesk.data.manifest import DataManifest, DatasetRecord
from algodesk.data.service import DataService
from algodesk.data.staleness import check_and_update_adjusted_staleness
from algodesk.data.store import BarStore

_TZ = ZoneInfo("Asia/Kolkata")


def _make_bar(d: date, close: float, vol: int = 1000, hour: int = 9, minute: int = 15) -> Bar:
    st = datetime(d.year, d.month, d.day, hour, minute, tzinfo=_TZ).astimezone(UTC)
    delta_days = 1 if hour == 9 and minute == 15 else 0
    delta_mins = 1 if minute != 15 else 0
    et = st + timedelta(days=delta_days, minutes=delta_mins)
    return Bar(
        instrument=InstrumentId("NSE:EQ:TEST"),
        timeframe="1d" if hour == 9 and minute == 15 else "1m",
        start_ts=st,
        end_ts=et,
        open=close - 1.0,
        high=close + 2.0,
        low=close - 2.0,
        close=close,
        volume=vol,
        complete=True,
    )


# -----------------------------------------------------------------------------
# 1. Golden Vector Tests
# -----------------------------------------------------------------------------
def test_golden_vector_split() -> None:
    # 5:1 split (old/new face value = 5 -> ratio=5.0)
    # Factor f = 1/r = 0.2. Volume divisor = 5.0 -> volume divided by r: 1000 // 5 = 200
    b1 = _make_bar(date(2026, 10, 1), 100.0, 1000)
    b2 = _make_bar(date(2026, 10, 2), 20.0, 5000)  # ex-date
    action = ActionRecord(
        ex_date=date(2026, 10, 2),
        type="split",
        ratio=5.0,
        source="upstox",
        source_ref="test",
        symbol="TEST",
        exchange="NSE",
        status="confirmed",
    )

    adj_bars, ids, a_hash = AdjustmentView.apply([b1, b2], [action], mode="splits", exchange_tz=_TZ)
    assert len(adj_bars) == 2
    assert pytest.approx(adj_bars[0].close, 0.001) == 20.0
    assert pytest.approx(adj_bars[0].open, 0.001) == 19.8
    assert adj_bars[0].volume == 200
    assert pytest.approx(adj_bars[1].close, 0.001) == 20.0
    assert adj_bars[1].volume == 5000
    assert len(ids) == 1
    assert len(a_hash) == 64


def test_golden_vector_bonus() -> None:
    # 1:1 bonus (ratio = (1+1)/1 = 2.0)
    # Factor f = 1/2 = 0.5. Volume divided by r: 1000 // 2 = 500
    b1 = _make_bar(date(2026, 10, 1), 200.0, 1000)
    b2 = _make_bar(date(2026, 10, 2), 100.0, 2000)  # ex-date
    action = ActionRecord(
        ex_date=date(2026, 10, 2),
        type="bonus",
        ratio=2.0,
        ratio_new=1.0,
        ratio_old=1.0,
        source="upstox",
        source_ref="test",
        symbol="TEST",
        exchange="NSE",
        status="confirmed",
    )

    adj_bars, _, _ = AdjustmentView.apply([b1, b2], [action], mode="splits", exchange_tz=_TZ)
    assert pytest.approx(adj_bars[0].close, 0.001) == 100.0
    assert adj_bars[0].volume == 500
    assert pytest.approx(adj_bars[1].close, 0.001) == 100.0
    assert adj_bars[1].volume == 2000


def test_golden_vector_dividend() -> None:
    # Dividend 10.0 on stock trading at 100.0 the day before.
    # Factor f = 1 - 10/100 = 0.9. Volume unchanged.
    b1 = _make_bar(date(2026, 10, 1), 100.0, 1000)
    b2 = _make_bar(date(2026, 10, 2), 90.0, 1200)  # ex-date
    action = ActionRecord(
        ex_date=date(2026, 10, 2),
        type="dividend",
        amount=Decimal("10.0"),
        source="yfinance",
        source_ref="test",
        symbol="TEST",
        exchange="NSE",
        status="confirmed",
    )

    adj_bars, _, _ = AdjustmentView.apply([b1, b2], [action], mode="total", exchange_tz=_TZ)
    assert pytest.approx(adj_bars[0].close, 0.001) == 90.0
    assert adj_bars[0].volume == 1000  # volume unchanged for dividend
    assert pytest.approx(adj_bars[1].close, 0.001) == 90.0


def test_golden_vector_rights() -> None:
    # 1:1 rights issue at 50. Prior close = 100.
    # TERP = (100 + 1*50)/(1 + 1) = 75. Factor f = 75/100 = 0.75. Volume unchanged.
    b1 = _make_bar(date(2026, 10, 1), 100.0, 1000)
    b2 = _make_bar(date(2026, 10, 2), 75.0, 1500)
    action = ActionRecord(
        ex_date=date(2026, 10, 2),
        type="rights",
        ratio=1.0,
        issue_price=Decimal("50.0"),
        source="operator",
        source_ref="override",
        symbol="TEST",
        exchange="NSE",
        status="confirmed",
    )

    adj_bars, _, _ = AdjustmentView.apply([b1, b2], [action], mode="total", exchange_tz=_TZ)
    assert pytest.approx(adj_bars[0].close, 0.001) == 75.0
    assert adj_bars[0].volume == 1000
    assert pytest.approx(adj_bars[1].close, 0.001) == 75.0


def test_stacked_events_multiply_backwards() -> None:
    # Event 1: 2:1 split on Oct 5 (factor 0.5)
    # Event 2: dividend 10.0 on Oct 10 (prior close on Oct 9 was 100.0 -> factor 0.9)
    # Bar Oct 1 is before both: factor = 0.5 * 0.9 = 0.45
    # Bar Oct 6 is after split, before dividend: factor = 0.9
    # Bar Oct 11 is after both: factor = 1.0
    b1 = _make_bar(date(2026, 10, 1), 200.0, 1000)
    b2 = _make_bar(date(2026, 10, 6), 100.0, 2000)
    b3 = _make_bar(date(2026, 10, 11), 90.0, 2000)

    a1 = ActionRecord(
        ex_date=date(2026, 10, 5),
        type="split",
        ratio=2.0,
        source="upstox",
        source_ref="s1",
        symbol="TEST",
        exchange="NSE",
        status="confirmed",
    )
    a2 = ActionRecord(
        ex_date=date(2026, 10, 10),
        type="dividend",
        amount=Decimal("10.0"),
        source="yfinance",
        source_ref="s2",
        symbol="TEST",
        exchange="NSE",
        status="confirmed",
    )

    adj_bars, ids, _ = AdjustmentView.apply([b1, b2, b3], [a1, a2], mode="total", exchange_tz=_TZ)
    assert len(ids) == 2
    assert pytest.approx(adj_bars[0].close, 0.001) == 200.0 * 0.45  # 90.0
    assert adj_bars[0].volume == 500  # split divided volume by 2
    assert pytest.approx(adj_bars[1].close, 0.001) == 100.0 * 0.9  # 90.0
    assert adj_bars[1].volume == 2000
    assert pytest.approx(adj_bars[2].close, 0.001) == 90.0


# -----------------------------------------------------------------------------
# 2. Timeframe Invariance
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("tf_minutes", [1, 5, 15, 30])
def test_timeframe_invariance(tf_minutes: int) -> None:
    # 2:1 split on Oct 5. Bars before Oct 5 are scaled by 0.5 regardless of intraday frequency.
    action = ActionRecord(
        ex_date=date(2026, 10, 5),
        type="split",
        ratio=2.0,
        source="upstox",
        source_ref="s",
        symbol="TEST",
        exchange="NSE",
        status="confirmed",
    )
    b_before = _make_bar(date(2026, 10, 2), 100.0, 500, hour=10, minute=tf_minutes)
    b_after = _make_bar(date(2026, 10, 6), 50.0, 1000, hour=10, minute=tf_minutes)

    adj_bars, _, _ = AdjustmentView.apply(
        [b_before, b_after], [action], mode="splits", exchange_tz=_TZ
    )
    assert pytest.approx(adj_bars[0].close, 0.001) == 50.0
    assert adj_bars[0].volume == 250
    assert pytest.approx(adj_bars[1].close, 0.001) == 50.0


# -----------------------------------------------------------------------------
# 3. Status Validation & Refusal
# -----------------------------------------------------------------------------
def test_conflict_refuses_naming_symbol_and_date() -> None:
    b = _make_bar(date(2026, 10, 1), 100.0)
    action = ActionRecord(
        ex_date=date(2026, 10, 2),
        type="split",
        ratio=2.0,
        source="upstox",
        source_ref="s",
        symbol="RELIANCE",
        exchange="NSE",
        status="conflict",
        reason="Upstox 2.0 vs yfinance 1.5",
    )
    with pytest.raises(DataError) as exc:
        AdjustmentView.apply([b], [action], mode="splits", exchange_tz=_TZ)
    err = str(exc.value)
    assert "RELIANCE" in err
    assert "2026-10-02" in err
    assert "conflict" in err


def test_manual_required_refuses_naming_symbol_and_date() -> None:
    b = _make_bar(date(2026, 10, 1), 100.0)
    action = ActionRecord(
        ex_date=date(2026, 10, 3),
        type="other",
        source="upstox",
        source_ref="s",
        symbol="TCS",
        exchange="NSE",
        status="manual_required",
        text="scheme of arrangement",
    )
    with pytest.raises(DataError) as exc:
        AdjustmentView.apply([b], [action], mode="total", exchange_tz=_TZ)
    err = str(exc.value)
    assert "TCS" in err
    assert "2026-10-03" in err
    assert "manual_required" in err


def test_unverified_refuses_without_flag() -> None:
    b = _make_bar(date(2026, 10, 1), 100.0)
    action = ActionRecord(
        ex_date=date(2026, 10, 2),
        type="split",
        ratio=2.0,
        source="upstox",
        source_ref="s",
        symbol="INFY",
        exchange="NSE",
        status="unverified",
    )
    with pytest.raises(DataError) as exc:
        AdjustmentView.apply([b], [action], mode="splits", exchange_tz=_TZ, accept_unverified=False)
    err = str(exc.value)
    assert "INFY" in err
    assert "unverified" in err


def test_unverified_accepted_with_flag() -> None:
    b = _make_bar(date(2026, 10, 1), 100.0)
    action = ActionRecord(
        ex_date=date(2026, 10, 2),
        type="split",
        ratio=2.0,
        source="upstox",
        source_ref="s",
        symbol="INFY",
        exchange="NSE",
        status="unverified",
    )
    adj_bars, ids, _ = AdjustmentView.apply(
        [b], [action], mode="splits", exchange_tz=_TZ, accept_unverified=True
    )
    assert pytest.approx(adj_bars[0].close, 0.001) == 50.0
    assert len(ids) == 1


# -----------------------------------------------------------------------------
# 4. DataService dynamic adjust modes & unknown basis refusal
# -----------------------------------------------------------------------------
class FakeAdjustmentBroker:
    def __init__(self, bars: list[Bar]) -> None:
        self._bars = bars

    def history(self, key: SeriesKey, start: datetime, end: datetime) -> Sequence[Bar]:
        return [b for b in self._bars if start <= b.start_ts <= end]


def test_dataservice_refuses_unknown_adjustment_basis(tmp_path: Path) -> None:
    m = DataManifest(tmp_path / "data_manifest.db")
    store = BarStore(tmp_path, manifest=m)
    broker = FakeAdjustmentBroker([_make_bar(date(2026, 10, 1), 100.0)])
    manifests = {
        "upstox": PluginManifest(
            name="upstox",
            kind="broker",
            version="1.0.0",
            api_version=1,
            entry="broker:Broker",
            capabilities={"history": True},
            candles_adjustment_basis="unknown",
        )
    }
    ds = DataService(
        sources={"upstox": broker}, default_source="upstox", store=store, manifests=manifests
    )
    key = SeriesKey(InstrumentId("NSE:EQ:RELIANCE"), "bars", "1d", "adjusted", "upstox")
    st = datetime(2026, 10, 1, 9, 15, tzinfo=UTC)
    et = datetime(2026, 10, 1, 15, 30, tzinfo=UTC)

    # Refuses when caller asks for adjusted series from a source whose basis is unknown
    with pytest.raises(DataError, match="unknown"):
        ds.get(key, st, et, download="adjusted", adjust="total")


def test_dataservice_raw_default_adjust_total(tmp_path: Path) -> None:
    m = DataManifest(tmp_path / "data_manifest.db")
    store = BarStore(tmp_path, manifest=m)
    b1 = _make_bar(date(2026, 10, 1), 200.0)
    b2 = _make_bar(date(2026, 10, 5), 100.0)
    broker = FakeAdjustmentBroker([b1, b2])
    manifests = {
        "yfinance": PluginManifest(
            name="yfinance",
            kind="broker",
            version="1.0.0",
            api_version=1,
            entry="broker:Broker",
            capabilities={"history": True},
            candles_adjustment_basis="total",
        )
    }
    ds = DataService(
        sources={"yfinance": broker}, default_source="yfinance", store=store, manifests=manifests
    )
    key = SeriesKey(InstrumentId("NSE:EQ:RELIANCE"), "bars", "1d", "raw", "yfinance")
    st = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    et = datetime(2026, 10, 6, 0, 0, tzinfo=UTC)

    # With no actions stored, raw bars returned with adjust=total
    res = ds.get(key, st, et, download="raw")
    assert len(res.bars) == 2
    assert res.provenance.get("adjust") == "total"


# -----------------------------------------------------------------------------
# 5. Staleness Path Tests (1d and 1m)
# -----------------------------------------------------------------------------
def test_staleness_1d_refetch(tmp_path: Path) -> None:
    m = DataManifest(tmp_path / "data_manifest.db")
    store = BarStore(tmp_path, manifest=m)
    key = SeriesKey(InstrumentId("NSE:EQ:RELIANCE"), "bars", "1d", "adjusted", "yfinance")
    ds_id = store.dataset_id(key)

    m.record_dataset(
        DatasetRecord(
            dataset_id=ds_id,
            instrument=str(key.instrument),
            kind=key.kind,
            timeframe=key.timeframe,
            adjustment=key.adjustment,
            source=key.source,
            schema_version=1,
            created_at=datetime.now(UTC).isoformat(),
            actions_epoch="2026-09-01",
        )
    )

    old_bar = _make_bar(date(2026, 9, 15), 100.0)
    store.commit(key, [old_bar], refresh=True)

    new_action = ActionRecord(
        ex_date=date(2026, 10, 1),
        type="split",
        ratio=2.0,
        source="yfinance",
        source_ref="s",
        symbol="RELIANCE",
        exchange="NSE",
        status="confirmed",
    )

    new_bar = _make_bar(date(2026, 9, 15), 50.0)  # refetched bar reflecting split
    broker = FakeAdjustmentBroker([new_bar])

    refreshed = check_and_update_adjusted_staleness(
        key=key,
        store=store,
        actions=[new_action],
        sources={"yfinance": broker},
    )
    assert refreshed is True
    updated_rec = m.get_dataset(ds_id)
    assert updated_rec is not None
    assert updated_rec.actions_epoch == "2026-10-01"
    read_slice = store.read(
        key, datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 2, tzinfo=UTC)
    )
    assert len(read_slice.bars) == 1
    assert pytest.approx(read_slice.bars[0].close, 0.001) == 50.0


def test_staleness_1m_factor_multiplication_with_audit(tmp_path: Path) -> None:
    m = DataManifest(tmp_path / "data_manifest.db")
    store = BarStore(tmp_path, manifest=m)
    key = SeriesKey(InstrumentId("NSE:EQ:RELIANCE"), "bars", "1m", "adjusted", "yfinance")
    ds_id = store.dataset_id(key)

    m.record_dataset(
        DatasetRecord(
            dataset_id=ds_id,
            instrument=str(key.instrument),
            kind=key.kind,
            timeframe=key.timeframe,
            adjustment=key.adjustment,
            source=key.source,
            schema_version=1,
            created_at=datetime.now(UTC).isoformat(),
            actions_epoch="2026-09-01",
        )
    )

    # Old 1m bar before the split ex-date
    old_bar = _make_bar(date(2026, 9, 15), 100.0, vol=500, hour=10, minute=30)
    store.commit(key, [old_bar], refresh=True)

    new_action = ActionRecord(
        ex_date=date(2026, 10, 1),
        type="split",
        ratio=2.0,
        source="yfinance",
        source_ref="s",
        symbol="RELIANCE",
        exchange="NSE",
        status="confirmed",
    )

    # Broker returns adjusted bar for sample audit (close=50.0)
    sample_bar = _make_bar(date(2026, 9, 15), 50.0, vol=250, hour=10, minute=30)
    broker = FakeAdjustmentBroker([sample_bar])

    refreshed = check_and_update_adjusted_staleness(
        key=key,
        store=store,
        actions=[new_action],
        sources={"yfinance": broker},
    )
    assert refreshed is True
    updated_rec = m.get_dataset(ds_id)
    assert updated_rec is not None
    assert updated_rec.actions_epoch == "2026-10-01"
    read_slice = store.read(
        key, datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 2, tzinfo=UTC)
    )
    assert len(read_slice.bars) == 1
    assert pytest.approx(read_slice.bars[0].close, 0.001) == 50.0
    assert read_slice.bars[0].volume == 250
