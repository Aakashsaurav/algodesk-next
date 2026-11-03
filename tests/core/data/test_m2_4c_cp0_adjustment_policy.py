"""Tests for CP0: relevant-events-only adjustment policy and actions audit."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from algodesk.api.errors import DataError
from algodesk.api.market import ActionRecord, Bar, InstrumentId, SeriesKey
from algodesk.cli.app import app
from algodesk.data.adjust import AdjustmentView
from algodesk.data.service import DataService, DataSlice


def _make_bar(d: date, close: float = 1000.0) -> Bar:
    dt = datetime(d.year, d.month, d.day, 9, 15, tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(UTC)
    return Bar(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        timeframe="1d",
        start_ts=dt,
        end_ts=dt + timedelta(days=1),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1000,
        complete=True,
    )


def test_1996_unverified_dividend_does_not_block_2025_read() -> None:
    bars = [_make_bar(date(2025, 1, 6), 1200.0), _make_bar(date(2025, 1, 7), 1210.0)]
    act_1996 = ActionRecord(
        ex_date=date(1996, 5, 24),
        type="dividend",
        amount=Decimal("2.5"),
        symbol="RELIANCE",
        exchange="NSE",
        status="unverified",
    )
    res = AdjustmentView.apply(
        bars,
        [act_1996],
        from_basis="splits",
        to_basis="total",
        accept_unverified=False,
    )
    assert len(res.bars) == 2
    assert res.bars[0].close == 1200.0
    assert res.bars[1].close == 1210.0
    assert res.action_ids == []


def test_action_before_first_bar_never_changes_prices_or_refuses() -> None:
    bars = [_make_bar(date(2025, 6, 1), 500.0)]
    # Actions prior to 2025-06-01: conflict, manual_required, unverified split
    acts = [
        ActionRecord(
            ex_date=date(2020, 1, 1),
            type="split",
            ratio=2.0,
            symbol="RELIANCE",
            exchange="NSE",
            status="conflict",
            reason="conflicting ratios",
        ),
        ActionRecord(
            ex_date=date(2021, 1, 1),
            type="other",
            symbol="RELIANCE",
            exchange="NSE",
            status="manual_required",
            reason="unknown event",
        ),
    ]
    res = AdjustmentView.apply(
        bars,
        acts,
        from_basis="splits",
        to_basis="none",
        accept_unverified=False,
    )
    assert len(res.bars) == 1
    assert res.bars[0].close == 500.0


def test_unverified_dividend_allowed_by_default_with_warning() -> None:
    bars = [_make_bar(date(2025, 1, 1), 100.0), _make_bar(date(2025, 1, 10), 100.0)]
    act = ActionRecord(
        ex_date=date(2025, 1, 5),
        type="dividend",
        amount=Decimal("10.0"),
        symbol="RELIANCE",
        exchange="NSE",
        status="unverified",
    )
    res = AdjustmentView.apply(
        bars,
        [act],
        from_basis="splits",
        to_basis="total",
        accept_unverified=False,
        allow_unverified_dividends=True,
    )
    assert len(res.bars) == 2
    assert any("Unverified dividend" in w for w in res.warnings)


def test_unverified_dividend_refuses_when_allow_unverified_dividends_false() -> None:
    bars = [_make_bar(date(2025, 1, 1), 100.0), _make_bar(date(2025, 1, 10), 100.0)]
    act = ActionRecord(
        ex_date=date(2025, 1, 5),
        type="dividend",
        amount=Decimal("10.0"),
        symbol="RELIANCE",
        exchange="NSE",
        status="unverified",
    )
    with pytest.raises(DataError, match="is unverified"):
        AdjustmentView.apply(
            bars,
            [act],
            from_basis="splits",
            to_basis="total",
            accept_unverified=False,
            allow_unverified_dividends=False,
        )

    res = AdjustmentView.apply(
        bars,
        [act],
        from_basis="splits",
        to_basis="total",
        accept_unverified=True,
        allow_unverified_dividends=False,
    )
    assert len(res.bars) == 2


def test_unverified_split_refuses_unless_accept_unverified() -> None:
    bars = [_make_bar(date(2025, 1, 1), 100.0), _make_bar(date(2025, 1, 10), 100.0)]
    act = ActionRecord(
        ex_date=date(2025, 1, 5),
        type="split",
        ratio=2.0,
        symbol="RELIANCE",
        exchange="NSE",
        status="unverified",
    )
    with pytest.raises(DataError, match="is unverified"):
        AdjustmentView.apply(
            bars,
            [act],
            from_basis="splits",
            to_basis="none",
            accept_unverified=False,
        )

    res = AdjustmentView.apply(
        bars,
        [act],
        from_basis="splits",
        to_basis="none",
        accept_unverified=True,
    )
    assert len(res.bars) == 2


def test_unverified_bonus_refuses_unless_accept_unverified() -> None:
    bars = [_make_bar(date(2025, 1, 1), 100.0), _make_bar(date(2025, 1, 10), 100.0)]
    act = ActionRecord(
        ex_date=date(2025, 1, 5),
        type="bonus",
        ratio=1.0,
        ratio_old=1.0,
        ratio_new=1.0,
        symbol="RELIANCE",
        exchange="NSE",
        status="unverified",
    )
    with pytest.raises(DataError, match="is unverified"):
        AdjustmentView.apply(
            bars,
            [act],
            from_basis="splits",
            to_basis="none",
            accept_unverified=False,
        )

    res = AdjustmentView.apply(
        bars,
        [act],
        from_basis="splits",
        to_basis="none",
        accept_unverified=True,
    )
    assert len(res.bars) == 2


def test_unverified_rights_refuses_unless_accept_unverified() -> None:
    bars = [_make_bar(date(2025, 1, 1), 100.0), _make_bar(date(2025, 1, 10), 100.0)]
    act = ActionRecord(
        ex_date=date(2025, 1, 5),
        type="rights",
        issue_price=Decimal("50.0"),
        ratio=1.0,
        symbol="RELIANCE",
        exchange="NSE",
        status="unverified",
    )
    with pytest.raises(DataError, match="is unverified"):
        AdjustmentView.apply(
            bars,
            [act],
            from_basis="splits",
            to_basis="total",
            accept_unverified=False,
        )

    res = AdjustmentView.apply(
        bars,
        [act],
        from_basis="splits",
        to_basis="total",
        accept_unverified=True,
    )
    assert len(res.bars) == 2


def test_unverified_other_refuses_unless_accept_unverified() -> None:
    bars = [_make_bar(date(2025, 1, 1), 100.0), _make_bar(date(2025, 1, 10), 100.0)]
    act = ActionRecord(
        ex_date=date(2025, 1, 5),
        type="other",
        symbol="RELIANCE",
        exchange="NSE",
        status="unverified",
    )
    with pytest.raises(DataError, match="is unverified"):
        AdjustmentView.apply(
            bars,
            [act],
            from_basis="splits",
            to_basis="total",
            accept_unverified=False,
        )

    res = AdjustmentView.apply(
        bars,
        [act],
        from_basis="splits",
        to_basis="total",
        accept_unverified=True,
    )
    assert len(res.bars) == 2


def test_conflict_and_manual_required_always_refuse_even_with_accept_unverified() -> None:
    bars = [_make_bar(date(2025, 1, 1), 100.0), _make_bar(date(2025, 1, 10), 100.0)]
    act_conflict = ActionRecord(
        ex_date=date(2025, 1, 5),
        type="dividend",
        amount=Decimal("10.0"),
        symbol="RELIANCE",
        exchange="NSE",
        status="conflict",
        reason="discrepancy between sources",
    )
    with pytest.raises(DataError, match="status 'conflict'"):
        AdjustmentView.apply(
            bars,
            [act_conflict],
            from_basis="splits",
            to_basis="total",
            accept_unverified=True,
        )

    act_manual = ActionRecord(
        ex_date=date(2025, 1, 5),
        type="split",
        ratio=2.0,
        symbol="RELIANCE",
        exchange="NSE",
        status="manual_required",
        reason="complex restructuring",
    )
    with pytest.raises(DataError, match="status 'manual_required'"):
        AdjustmentView.apply(
            bars,
            [act_manual],
            from_basis="splits",
            to_basis="total",
            accept_unverified=True,
        )


def test_data_actions_audit_missing_series_prints_data_get_command(
    monkeypatch: Any,
    tmp_project: Path,
) -> None:
    runner = CliRunner()
    inst = InstrumentId("NSE:EQ:RELIANCE")
    k_raw = SeriesKey(inst, "bars", "1d", "raw", "yfinance")
    k_adj = SeriesKey(inst, "bars", "1d", "adjusted", "yfinance")
    t0 = datetime(2025, 1, 1, tzinfo=UTC)

    # Return empty bars for raw download to test missing command
    def fake_get(
        self: Any, series: Any, start: Any = None, end: Any = None, **kwargs: Any
    ) -> DataSlice:
        dl = kwargs.get("download", "adjusted")
        if dl == "raw":
            return DataSlice(key=k_raw, start=t0, end=t0, bars=())
        bar = _make_bar(date(2025, 1, 5), 1000.0)
        return DataSlice(key=k_adj, start=t0, end=t0, bars=(bar,))

    monkeypatch.setattr(DataService, "get", fake_get)

    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "actions",
            "audit",
            "RELIANCE",
            "--from",
            "2025-01-01",
            "--to",
            "2025-01-10",
        ],
    )
    assert res.exit_code == 0
    assert "Missing series command:" in res.output
    exp_cmd = "algodesk data get RELIANCE 1d 2025-01-01 2025-01-10 --source yfinance --download raw"
    assert exp_cmd in res.output


def test_data_actions_audit_max_and_median_diff(
    monkeypatch: Any,
    tmp_project: Path,
) -> None:
    runner = CliRunner()
    inst = InstrumentId("NSE:EQ:RELIANCE")
    dt1 = datetime(2025, 1, 2, tzinfo=UTC)
    dt2 = datetime(2025, 1, 3, tzinfo=UTC)

    b_raw1 = _make_bar(date(2025, 1, 2), 1000.0)
    b_raw2 = _make_bar(date(2025, 1, 3), 1000.0)
    # Adjusted bars with known differences: 1% and 3%
    b_adj1 = _make_bar(date(2025, 1, 2), 990.0)  # ~1.01%
    b_adj2 = _make_bar(date(2025, 1, 3), 970.0)  # ~3.09%

    def fake_get(
        self: Any, series: Any, start: Any = None, end: Any = None, **kwargs: Any
    ) -> DataSlice:
        dl = kwargs.get("download", "adjusted")
        bars = (b_raw1, b_raw2) if dl == "raw" else (b_adj1, b_adj2)
        k = SeriesKey(inst, "bars", "1d", dl, "yfinance")
        return DataSlice(key=k, start=dt1, end=dt2, bars=bars)

    monkeypatch.setattr(DataService, "get", fake_get)

    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "actions",
            "audit",
            "RELIANCE",
            "--from",
            "2025-01-01",
            "--to",
            "2025-01-10",
        ],
    )
    assert res.exit_code == 0
    assert "Max Diff" in res.output
    assert "Median Diff" in res.output
    assert "FLAG" in res.output


def test_allow_unverified_dividends_false_refuses() -> None:
    bars = [_make_bar(date(2025, 1, 1), 100.0), _make_bar(date(2025, 1, 10), 100.0)]
    act = ActionRecord(
        ex_date=date(2025, 1, 5),
        type="dividend",
        amount=Decimal("10.0"),
        symbol="RELIANCE",
        exchange="NSE",
        status="unverified",
    )
    with pytest.raises(DataError, match="is unverified"):
        AdjustmentView.apply(
            bars,
            [act],
            from_basis="splits",
            to_basis="total",
            accept_unverified=False,
            allow_unverified_dividends=False,
        )


def test_conflict_action_refuses_even_with_accept_unverified() -> None:
    bars = [_make_bar(date(2025, 1, 1), 100.0), _make_bar(date(2025, 1, 10), 100.0)]
    act = ActionRecord(
        ex_date=date(2025, 1, 5),
        type="split",
        ratio=2.0,
        symbol="RELIANCE",
        exchange="NSE",
        status="conflict",
        reason="Source discrepancy",
    )
    with pytest.raises(DataError, match="status 'conflict'"):
        AdjustmentView.apply(
            bars,
            [act],
            from_basis="splits",
            to_basis="none",
            accept_unverified=True,
        )


def test_manual_required_action_refuses_even_with_accept_unverified() -> None:
    bars = [_make_bar(date(2025, 1, 1), 100.0), _make_bar(date(2025, 1, 10), 100.0)]
    act = ActionRecord(
        ex_date=date(2025, 1, 5),
        type="bonus",
        ratio=1.0,
        symbol="RELIANCE",
        exchange="NSE",
        status="manual_required",
        reason="Ambiguous corporate announcement",
    )
    with pytest.raises(DataError, match="status 'manual_required'"):
        AdjustmentView.apply(
            bars,
            [act],
            from_basis="splits",
            to_basis="none",
            accept_unverified=True,
        )


def test_rights_action_adjustment_computation() -> None:
    bars = [_make_bar(date(2025, 1, 1), 100.0), _make_bar(date(2025, 1, 10), 100.0)]
    act = ActionRecord(
        ex_date=date(2025, 1, 5),
        type="rights",
        ratio=0.5,
        issue_price=Decimal("50.0"),
        symbol="RELIANCE",
        exchange="NSE",
        status="verified",
    )
    res = AdjustmentView.apply(
        bars,
        [act],
        from_basis="splits",
        to_basis="total",
    )
    assert len(res.bars) == 2
    # p=100, r=0.5, p_issue=50 -> terp=(100 + 25)/1.5 = 83.33.
    # factor = terp/p = 83.3333/100 = 0.83333
    assert round(res.bars[0].close, 2) == 83.33
    assert res.bars[1].close == 100.0


def test_bonus_and_split_compounding() -> None:
    bars = [_make_bar(date(2025, 1, 1), 100.0), _make_bar(date(2025, 1, 10), 100.0)]
    acts = [
        ActionRecord(
            ex_date=date(2025, 1, 3),
            type="split",
            ratio=2.0,
            symbol="RELIANCE",
            exchange="NSE",
            status="verified",
        ),
        ActionRecord(
            ex_date=date(2025, 1, 7),
            type="bonus",
            ratio=1.0,
            symbol="RELIANCE",
            exchange="NSE",
            status="verified",
        ),
    ]
    res = AdjustmentView.apply(
        bars,
        acts,
        from_basis="splits",
        to_basis="none",
    )
    assert len(res.bars) == 2
    assert res.bars[0].close == 400.0
    assert res.bars[1].close == 100.0


def test_data_actions_audit_zero_diff_clean(
    monkeypatch: Any,
    tmp_project: Path,
) -> None:
    runner = CliRunner()
    inst = InstrumentId("NSE:EQ:RELIANCE")
    dt1 = datetime(2025, 1, 2, tzinfo=UTC)
    dt2 = datetime(2025, 1, 3, tzinfo=UTC)

    b1 = _make_bar(date(2025, 1, 2), 1000.0)
    b2 = _make_bar(date(2025, 1, 3), 1000.0)

    def fake_get(
        self: Any, series: Any, start: Any = None, end: Any = None, **kwargs: Any
    ) -> DataSlice:
        dl = kwargs.get("download", "adjusted")
        k = SeriesKey(inst, "bars", "1d", dl, "yfinance")
        return DataSlice(key=k, start=dt1, end=dt2, bars=(b1, b2))

    monkeypatch.setattr(DataService, "get", fake_get)

    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "actions",
            "audit",
            "RELIANCE",
            "--from",
            "2025-01-01",
            "--to",
            "2025-01-10",
        ],
    )
    assert res.exit_code == 0
    assert "0.00%" in res.output
    assert "Audit completed clean" in res.output
