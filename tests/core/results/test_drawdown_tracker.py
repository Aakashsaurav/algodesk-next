"""Tests for Step 3: Incremental DrawdownTracker property tests and backward compatibility."""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from algodesk.results.metrics import DrawdownTracker, compute_drawdown_metrics


def _reference_compute_drawdown_metrics(
    equity_series: Sequence[Decimal | float],
    timestamps: Sequence[datetime] | None = None,
) -> tuple[float, int]:
    """Exact reference copy of the old O(N^2) compute_drawdown_metrics implementation."""
    if not equity_series:
        return 0.0, 0
    equities = [float(x) for x in equity_series]
    max_dd_pct = 0.0
    peak = equities[0]
    peak_idx = 0
    in_drawdown = False
    max_duration_days = 0

    def _ref_measure_dur(ts: Sequence[datetime] | None, s_idx: int, e_idx: int) -> int:
        if ts and len(ts) > e_idx:
            dt0 = ts[s_idx]
            dt1 = ts[e_idx]
            return max(0, (dt1.date() - dt0.date()).days)
        return max(0, e_idx - s_idx)

    for i, eq in enumerate(equities):
        if eq > peak:
            if in_drawdown:
                dur = _ref_measure_dur(timestamps, peak_idx, i)
                if dur > max_duration_days:
                    max_duration_days = dur
                in_drawdown = False
            peak = eq
            peak_idx = i
        elif eq == peak:
            if in_drawdown:
                dur = _ref_measure_dur(timestamps, peak_idx, i)
                if dur > max_duration_days:
                    max_duration_days = dur
                in_drawdown = False
            peak_idx = i
        else:
            in_drawdown = True
            dd = (peak - eq) / peak * 100.0
            if dd > max_dd_pct:
                max_dd_pct = dd
            dur = _ref_measure_dur(timestamps, peak_idx, i)
            if dur > max_duration_days:
                max_duration_days = dur

    return round(max_dd_pct, 4), max_duration_days


def test_drawdown_tracker_empty_series() -> None:
    """Empty series returns 0.0 max dd and 0 duration."""
    tracker = DrawdownTracker()
    assert tracker.max_drawdown == 0.0
    assert tracker.max_duration == 0
    assert compute_drawdown_metrics([]) == (0.0, 0)


def test_drawdown_tracker_single_point() -> None:
    """Single point equity curve returns 0.0 drawdown and 0 duration."""
    series = [100000.0]
    expected = _reference_compute_drawdown_metrics(series)
    assert compute_drawdown_metrics(series) == expected
    assert expected == (0.0, 0)


def test_drawdown_tracker_flat_series() -> None:
    """Flat equity curve across multiple points has 0.0 drawdown and 0 duration."""
    series = [100000.0] * 50
    expected = _reference_compute_drawdown_metrics(series)
    assert compute_drawdown_metrics(series) == expected
    assert expected == (0.0, 0)


def test_drawdown_tracker_monotonic_increasing() -> None:
    """Monotonically increasing equity curve never experiences drawdown."""
    series = [100.0 + i * 5.0 for i in range(100)]
    expected = _reference_compute_drawdown_metrics(series)
    assert compute_drawdown_metrics(series) == expected
    assert expected == (0.0, 0)


def test_drawdown_tracker_monotonic_decreasing() -> None:
    """Monotonically decreasing equity curve reaches peak at first element."""
    series = [100.0 - i * 0.5 for i in range(100)]
    expected = _reference_compute_drawdown_metrics(series)
    assert compute_drawdown_metrics(series) == expected
    assert expected[0] == round((100.0 - 50.5) / 100.0 * 100.0, 4)


def test_drawdown_tracker_with_timestamps() -> None:
    """Drawdown with calendar timestamps computes exact calendar day duration."""
    t0 = datetime(2026, 1, 1, 9, 15, tzinfo=UTC)
    timestamps = [t0 + timedelta(days=i) for i in range(5)]
    series = [100.0, 80.0, 70.0, 90.0, 110.0]

    expected = _reference_compute_drawdown_metrics(series, timestamps)
    actual = compute_drawdown_metrics(series, timestamps)
    assert actual == expected
    assert actual[0] == 30.0  # (100 - 70) / 100 * 100%
    assert actual[1] == 4  # recovered on day 4


def test_drawdown_tracker_incremental_snapshot_state() -> None:
    """Incremental tracker exposes running peak, current drawdown, and max drawdown."""
    tracker = DrawdownTracker()
    max_dd, curr_dd, dur = tracker.update(100.0)
    assert max_dd == 0.0 and curr_dd == 0.0 and dur == 0

    max_dd, curr_dd, dur = tracker.update(90.0)
    assert max_dd == 10.0 and curr_dd == 10.0 and dur == 1

    max_dd, curr_dd, dur = tracker.update(80.0)
    assert max_dd == 20.0 and curr_dd == 20.0 and dur == 2

    # Partial recovery
    max_dd, curr_dd, dur = tracker.update(95.0)
    assert max_dd == 20.0 and curr_dd == 5.0 and dur == 3

    # Full recovery to new high
    max_dd, curr_dd, dur = tracker.update(105.0)
    assert max_dd == 20.0 and curr_dd == 0.0 and dur == 4


@pytest.mark.parametrize("seed", [42, 123, 999, 2026])
def test_drawdown_tracker_property_matches_reference_random_series(seed: int) -> None:
    """Property test: DrawdownTracker strictly equals reference implementation on random series."""
    rng = random.Random(seed)
    n = 200
    val = 1000.0
    series: list[float] = [val]
    t0 = datetime(2026, 1, 1, 9, 15, tzinfo=UTC)
    timestamps = [t0]

    for i in range(1, n):
        step = rng.uniform(-50.0, 50.0)
        val = max(10.0, val + step)
        series.append(round(val, 2))
        timestamps.append(t0 + timedelta(days=i))

    # Test without timestamps
    ref_no_ts = _reference_compute_drawdown_metrics(series, None)
    act_no_ts = compute_drawdown_metrics(series, None)
    assert act_no_ts == ref_no_ts

    # Test with timestamps
    ref_ts = _reference_compute_drawdown_metrics(series, timestamps)
    act_ts = compute_drawdown_metrics(series, timestamps)
    assert act_ts == ref_ts


def test_drawdown_tracker_decimal_inputs() -> None:
    """DrawdownTracker accepts Decimal inputs seamlessly."""
    series = [Decimal("100.0"), Decimal("85.5"), Decimal("92.0"), Decimal("105.0")]
    max_dd, dur = compute_drawdown_metrics(series)
    assert max_dd == 14.5
    assert dur == 3
