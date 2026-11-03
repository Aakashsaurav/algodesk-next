"""Tests for M2-4d CP0: action verification scope per basis transition and --accept-unverified."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from algodesk.api.errors import DataError
from algodesk.api.market import ActionRecord, Bar, InstrumentId
from algodesk.data.adjust import AdjustmentView, _used_action_types


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


def test_used_action_types_per_transition() -> None:
    assert _used_action_types("splits", "total") == {"dividend", "rights", "other"}
    assert _used_action_types("splits", "none") == {"split", "bonus", "other"}
    assert _used_action_types("none", "splits") == {"split", "bonus", "other"}
    assert _used_action_types("none", "total") == {"split", "bonus", "dividend", "rights", "other"}


def test_unverified_bonus_does_not_block_splits_to_total() -> None:
    bars = [_make_bar(date(2024, 1, 1), 1000.0), _make_bar(date(2024, 11, 1), 1000.0)]
    act_bonus = ActionRecord(
        id="act_bonus_20241028",
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0,
        ratio_new=1,
        ratio_old=1,
        symbol="RELIANCE",
        exchange="NSE",
        status="unverified",
    )

    # 1. splits -> total uses dividends only; bonus should NOT block
    res = AdjustmentView.apply(
        bars,
        [act_bonus],
        from_basis="splits",
        to_basis="total",
        accept_unverified=False,
    )
    assert len(res.bars) == 2
    assert res.bars[0].close == 1000.0


def test_unverified_bonus_blocks_splits_to_none_unless_accepted() -> None:
    bars = [_make_bar(date(2024, 1, 1), 1000.0), _make_bar(date(2024, 11, 1), 1000.0)]
    act_bonus = ActionRecord(
        id="act_bonus_20241028",
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0,
        ratio_new=1,
        ratio_old=1,
        symbol="RELIANCE",
        exchange="NSE",
        status="unverified",
    )

    # 2. splits -> none uses splits/bonus; unverified bonus MUST block
    with pytest.raises(DataError) as exc_info:
        AdjustmentView.apply(
            bars,
            [act_bonus],
            from_basis="splits",
            to_basis="none",
            accept_unverified=False,
        )
    assert "Action bonus on 2024-10-28 for RELIANCE is unverified" in exc_info.value.what

    # 3. With accept_unverified=True, it succeeds and provenance warning names the action
    res_acc = AdjustmentView.apply(
        bars,
        [act_bonus],
        from_basis="splits",
        to_basis="none",
        accept_unverified=True,
    )
    assert len(res_acc.bars) == 2
    assert any(
        "Unverified bonus on 2024-10-28 for RELIANCE (id: act_bonus_20241028) applied" in w
        for w in res_acc.warnings
    )


def test_unverified_dividend_does_not_block_none_to_splits() -> None:
    bars = [_make_bar(date(2024, 1, 1), 1000.0), _make_bar(date(2024, 11, 1), 1000.0)]
    act_div = ActionRecord(
        id="act_div_1",
        ex_date=date(2024, 6, 15),
        type="dividend",
        amount=Decimal("10.0"),
        symbol="RELIANCE",
        exchange="NSE",
        status="unverified",
    )
    # none -> splits uses split/bonus only, so unverified dividend must NOT block
    res = AdjustmentView.apply(
        bars,
        [act_div],
        from_basis="none",
        to_basis="splits",
        accept_unverified=False,
        allow_unverified_dividends=False,
    )
    assert len(res.bars) == 2
    assert res.bars[0].close == 1000.0


def test_none_to_total_requires_both() -> None:
    bars = [_make_bar(date(2024, 1, 1), 1000.0), _make_bar(date(2024, 11, 1), 1000.0)]
    act_split = ActionRecord(
        id="act_split_1",
        ex_date=date(2024, 5, 10),
        type="split",
        ratio=2.0,
        symbol="RELIANCE",
        exchange="NSE",
        status="unverified",
    )
    # none -> total uses splits/bonus + dividends, so unverified split MUST block
    with pytest.raises(DataError) as exc_info:
        AdjustmentView.apply(
            bars,
            [act_split],
            from_basis="none",
            to_basis="total",
            accept_unverified=False,
        )
    assert "Action split on 2024-05-10 for RELIANCE is unverified" in exc_info.value.what


def test_provenance_warning_names_unverified_action_details() -> None:
    bars = [_make_bar(date(2024, 1, 1), 100.0), _make_bar(date(2024, 11, 1), 100.0)]
    act_div = ActionRecord(
        id="act_div_rel_2024",
        ex_date=date(2024, 8, 19),
        type="dividend",
        amount=Decimal("10.0"),
        symbol="RELIANCE",
        exchange="NSE",
        status="unverified",
    )
    res = AdjustmentView.apply(
        bars,
        [act_div],
        from_basis="splits",
        to_basis="total",
        accept_unverified=True,
    )
    assert len(res.warnings) == 1
    w = res.warnings[0]
    assert "Unverified dividend" in w
    assert "2024-08-19" in w
    assert "RELIANCE" in w
    assert "act_div_rel_2024" in w


def test_conflict_and_manual_required_fail_closed_across_all_transitions() -> None:
    """Conflict and manual_required actions always fail closed regardless of transition type."""
    bars = [_make_bar(date(2024, 1, 1), 100.0), _make_bar(date(2024, 11, 1), 100.0)]
    act_conflict = ActionRecord(
        id="act_conf_1",
        ex_date=date(2024, 5, 10),
        type="bonus",
        ratio=Decimal("0.5"),
        status="conflict",
    )
    # Even splits to total (which uses dividends) must refuse when conflict is present in range
    with pytest.raises(DataError) as exc_info:
        AdjustmentView.apply(bars, [act_conflict], from_basis="splits", to_basis="total")
    assert "conflict" in exc_info.value.what

    act_manual = ActionRecord(
        id="act_man_1",
        ex_date=date(2024, 5, 10),
        type="bonus",
        ratio=Decimal("0.5"),
        status="manual_required",
    )
    with pytest.raises(DataError) as exc_info2:
        AdjustmentView.apply(bars, [act_manual], from_basis="splits", to_basis="total")
    assert "manual_required" in exc_info2.value.what


def test_unverified_split_blocks_none_to_splits_and_splits_to_none() -> None:
    """Unverified split blocks transitions that use splits, but does not block splits to total."""
    bars = [_make_bar(date(2024, 1, 1), 100.0), _make_bar(date(2024, 11, 1), 100.0)]
    act_split = ActionRecord(
        id="act_split_1",
        ex_date=date(2024, 4, 1),
        type="split",
        ratio=Decimal("2.0"),
        status="unverified",
    )
    # splits to total uses dividends, not splits -> succeeds
    res = AdjustmentView.apply(bars, [act_split], from_basis="splits", to_basis="total")
    assert len(res.bars) == 2

    # none to splits uses splits -> raises DataError unless accepted
    with pytest.raises(DataError) as exc_info:
        AdjustmentView.apply(bars, [act_split], from_basis="none", to_basis="splits")
    assert "unverified" in exc_info.value.what

    # splits to none uses splits -> raises DataError unless accepted
    with pytest.raises(DataError) as exc_info2:
        AdjustmentView.apply(bars, [act_split], from_basis="splits", to_basis="none")
    assert "unverified" in exc_info2.value.what


def test_unverified_action_with_accept_unverified_returns_adjusted_bars() -> None:
    """When accept_unverified=True, transition succeeds and applies ratio with warning."""
    bars = [_make_bar(date(2024, 1, 1), 100.0), _make_bar(date(2024, 11, 1), 100.0)]
    act_split = ActionRecord(
        id="act_split_accept",
        ex_date=date(2024, 5, 1),
        type="split",
        ratio=Decimal("2.0"),
        symbol="TCS",
        status="unverified",
    )
    res = AdjustmentView.apply(
        bars,
        [act_split],
        from_basis="none",
        to_basis="splits",
        accept_unverified=True,
    )
    assert len(res.bars) == 2
    assert res.bars[0].close == Decimal("50.0")  # Adjusted by split ratio
    assert len(res.warnings) == 1
    assert "act_split_accept" in res.warnings[0]
