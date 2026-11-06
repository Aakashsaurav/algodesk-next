"""Tests for M2-8 CP1 Step 2: Closure inference presence-only path and Bhavcopy precedence."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from algodesk.api.market import ActionRecord, Bar, InstrumentId, SeriesKey
from algodesk.data.actions_store import ActionsStore
from algodesk.data.closures import _try_bhavcopy_presence, infer_market_closures
from algodesk.data.manifest import DataManifest
from algodesk.data.service import DataService
from algodesk.data.store import BarStore

_IST = ZoneInfo("Asia/Kolkata")


def test_data_service_get_presence_only_skips_adjustments(tmp_project: Path) -> None:
    """presence_only=True returns stored raw bars without checking corporate actions."""
    manifest = DataManifest(tmp_project / "data" / "manifest.db")
    store = BarStore(tmp_project / "data", manifest, exchange_tz=_IST)
    ds = DataService(sources={}, store=store, exchange_tz=_IST)

    inst = InstrumentId("NSE:EQ:DEMERGSYM")
    key = SeriesKey(inst, "bars", "1d", "raw", "yfinance")
    b1 = Bar(
        inst,
        "1d",
        datetime(2023, 7, 19, 9, 15, tzinfo=UTC),
        datetime(2023, 7, 19, 9, 15, tzinfo=UTC),
        100.0,
        105.0,
        95.0,
        100.0,
        1000,
        True,
        False,
    )
    b2 = Bar(
        inst,
        "1d",
        datetime(2023, 7, 20, 9, 15, tzinfo=UTC),
        datetime(2023, 7, 20, 9, 15, tzinfo=UTC),
        100.0,
        105.0,
        95.0,
        100.0,
        1000,
        True,
        False,
    )
    b3 = Bar(
        inst,
        "1d",
        datetime(2023, 7, 21, 9, 15, tzinfo=UTC),
        datetime(2023, 7, 21, 9, 15, tzinfo=UTC),
        100.0,
        105.0,
        95.0,
        100.0,
        1000,
        True,
        False,
    )
    store.commit(key, [b1, b2, b3])

    # Record a manual_required action on the exact date
    a_store = ActionsStore(data_dir=tmp_project / "data", manifest=manifest)
    act = ActionRecord(
        ex_date=date(2023, 7, 20),
        type="other",
        ratio=None,
        source="yfinance",
        symbol="DEMERGSYM",
        status="manual_required",
    )
    a_store.write("NSE", "DEMERGSYM", [act])

    # Calling with presence_only=True succeeds without raising DataError
    res = ds.get(
        key,
        start=datetime(2023, 7, 19, tzinfo=UTC),
        end=datetime(2023, 7, 21, tzinfo=UTC),
        download="raw",
        presence_only=True,
    )
    assert len(res.bars) == 3
    assert res.bars[0].close == 100.0
    assert res.provenance.get("presence_only") is True


def test_closure_inference_presence_only_handles_manual_required_action(tmp_project: Path) -> None:
    """infer_market_closures runs presence-only, not failing on manual_required actions."""
    manifest = DataManifest(tmp_project / "data" / "manifest.db")
    mock_ds = MagicMock()
    mock_ds._exchange_tz = _IST
    mock_ds._default_source = "yfinance"
    mock_ds._sources = {}

    inst = InstrumentId("NSE:EQ:DEMERGSYM")
    b_date = datetime(2023, 7, 20, 9, 15, tzinfo=UTC)
    b = Bar(inst, "1d", b_date, b_date, 100.0, 105.0, 95.0, 100.0, 1000, True, False)

    slice_mock = MagicMock()
    slice_mock.bars = [b]
    mock_ds.get.return_value = slice_mock

    # Run inference across date range with manual_required demerger date 2023-07-20
    recs = infer_market_closures(
        manifest=manifest,
        data_service=mock_ds,
        exchange="NSE",
        start_d=date(2023, 7, 19),
        end_d=date(2023, 7, 21),
        sample_symbols=["NSE:EQ:DEMERGSYM"],
        dry_run=True,
    )
    assert recs is not None
    # Verify presence_only=True was passed to data_service.get
    assert mock_ds.get.called
    _, kwargs = mock_ds.get.call_args
    assert kwargs.get("presence_only") is True


def test_closure_inference_demerger_date_in_range(tmp_project: Path) -> None:
    """Inference over 2023-07-15 to 2023-07-25 (including Reliance demerger 2023-07-20) succeeds."""
    manifest = DataManifest(tmp_project / "data" / "manifest.db")
    store = BarStore(tmp_project / "data", manifest, exchange_tz=_IST)
    ds = DataService(sources={}, store=store, exchange_tz=_IST)

    inst = InstrumentId("NSE:EQ:RELIANCE")
    key = SeriesKey(inst, "bars", "1d", "raw", "yfinance")

    # Ingest bars for weekdays
    bars: list[Bar] = []
    cur = date(2023, 7, 17)
    while cur <= date(2023, 7, 21):
        dt = datetime(cur.year, cur.month, cur.day, 9, 15, tzinfo=UTC)
        bars.append(Bar(inst, "1d", dt, dt, 2500.0, 2550.0, 2480.0, 2500.0, 5000, True, False))
        cur += timedelta(days=1)
    store.commit(key, bars)

    # Ingest manual_required demerger action on 2023-07-20
    a_store = ActionsStore(data_dir=tmp_project / "data", manifest=manifest)
    act = ActionRecord(
        ex_date=date(2023, 7, 20),
        type="other",
        ratio=None,
        source="yfinance",
        symbol="RELIANCE",
        status="manual_required",
    )
    a_store.write("NSE", "RELIANCE", [act])

    # Infer closures across range containing demerger
    recs = infer_market_closures(
        manifest=manifest,
        data_service=ds,
        exchange="NSE",
        start_d=date(2023, 7, 17),
        end_d=date(2023, 7, 21),
        sample_symbols=["NSE:EQ:RELIANCE"],
        dry_run=True,
    )
    # No closed days because bars exist on all weekdays
    inferred_closed = [r for r in recs if r.kind == "inferred"]
    assert len(inferred_closed) == 0


def test_closure_inference_uses_bhavcopy_precedence_for_nse() -> None:
    """For NSE, bhavcopy daily_bars is used first when available."""
    mock_ds = MagicMock()
    mock_ds._exchange_tz = _IST
    mock_bhav = MagicMock()
    inst = InstrumentId("NSE:EQ:TCS")
    dt = datetime(2024, 10, 25, 9, 15, tzinfo=UTC)
    mock_bhav.daily_bars.return_value = [
        Bar(inst, "1d", dt, dt, 3000.0, 3050.0, 2980.0, 3000.0, 100, True, False)
    ]
    mock_ds._sources = {"nse_bhavcopy": mock_bhav}

    from collections import defaultdict

    sym_bars_map: dict[str, set[date]] = defaultdict(set)
    valid_chunks: dict[str, set[date]] = defaultdict(set)

    res = _try_bhavcopy_presence(
        mock_ds,
        ["TCS"],
        date(2024, 10, 25),
        date(2024, 10, 25),
        _IST,
        sym_bars_map,
        valid_chunks,
    )
    assert res is True
    assert mock_bhav.daily_bars.called
    assert date(2024, 10, 25) in sym_bars_map["TCS"]


def test_closure_inference_falls_back_when_bhavcopy_unreachable(tmp_project: Path) -> None:
    """When bhavcopy throws an exception (unreachable), infer falls back to sample symbols."""
    manifest = DataManifest(tmp_project / "data" / "manifest.db")
    mock_ds = MagicMock()
    mock_ds._exchange_tz = _IST
    mock_ds._default_source = "yfinance"
    mock_ds._sources = {}

    mock_bhav = MagicMock()
    mock_bhav.daily_bars.side_effect = RuntimeError("Bhavcopy server unreachable")
    mock_ds._sources = {"nse_bhavcopy": mock_bhav}

    inst = InstrumentId("NSE:EQ:INFY")
    dt = datetime(2024, 5, 2, 9, 15, tzinfo=UTC)
    slice_mock = MagicMock()
    slice_mock.bars = [Bar(inst, "1d", dt, dt, 1500.0, 1520.0, 1490.0, 1500.0, 100, True, False)]
    mock_ds.get.return_value = slice_mock

    recs = infer_market_closures(
        manifest=manifest,
        data_service=mock_ds,
        exchange="NSE",
        start_d=date(2024, 5, 2),
        end_d=date(2024, 5, 2),
        sample_symbols=["NSE:EQ:INFY"],
        dry_run=True,
    )
    # Successfully fell back to mock_ds.get
    assert mock_ds.get.called
    assert len(recs) == 0
