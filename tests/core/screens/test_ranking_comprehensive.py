"""Comprehensive tests for ranking calculations, ties, and edge cases."""

from __future__ import annotations

import pytest

from algodesk.api.screens import ScreenRow
from algodesk.screens.models import OperandModel, RankSpec
from algodesk.screens.ranking import apply_ranking, compute_percentile_ranks


def test_percentile_ten_symbols() -> None:
    vals = {f"S{i}": float(i * 10) for i in range(1, 11)}
    ranks = compute_percentile_ranks(vals)
    assert len(ranks) == 10
    for i in range(1, 11):
        assert ranks[f"S{i}"] == pytest.approx(i * 10.0, abs=1e-3)


def test_percentile_half_tied() -> None:
    # 6 symbols: 3 with value 10.0, 3 with value 20.0
    # For value 10: 3/6 -> 50.0%
    # For value 20: 6/6 -> 100.0%
    vals = {"A1": 10.0, "A2": 10.0, "A3": 10.0, "B1": 20.0, "B2": 20.0, "B3": 20.0}
    ranks = compute_percentile_ranks(vals)
    for a in ("A1", "A2", "A3"):
        assert ranks[a] == 50.0
    for b in ("B1", "B2", "B3"):
        assert ranks[b] == 100.0


def test_rank_min_zero_accepts_all() -> None:
    rows = [ScreenRow(symbol="S1", passed=True), ScreenRow(symbol="S2", passed=True)]
    spec = RankSpec(by=OperandModel(field="close"), min=0.0)
    updated = apply_ranking(rows, spec, {"S1": 10.0, "S2": 20.0})
    assert all(r.passed for r in updated)


def test_rank_min_100_accepts_only_top() -> None:
    rows = [
        ScreenRow(symbol="S1", passed=True),
        ScreenRow(symbol="S2", passed=True),
        ScreenRow(symbol="S3", passed=True),
    ]
    spec = RankSpec(by=OperandModel(field="close"), min=100.0)
    updated = apply_ranking(rows, spec, {"S1": 10.0, "S2": 20.0, "S3": 30.0})
    by_s = {r.symbol: r.passed for r in updated}
    assert by_s["S1"] is False
    assert by_s["S2"] is False
    assert by_s["S3"] is True


def test_rank_preserves_unranked_passed_status() -> None:
    # A symbol that previously failed screen conditions remains failed even if rank is 100%
    rows = [ScreenRow(symbol="S1", passed=False)]
    spec = RankSpec(by=OperandModel(field="close"), min=50.0)
    updated = apply_ranking(rows, spec, {"S1": 100.0})
    assert updated[0].passed is False
    assert updated[0].score == 100.0


def test_rank_missing_metric_value_defaults_score() -> None:
    rows = [ScreenRow(symbol="S1", passed=True)]
    spec = RankSpec(by=OperandModel(field="close"))
    # S1 not in metric_values
    updated = apply_ranking(rows, spec, {})
    assert updated[0].score == 0.0


def test_rank_float_precision_rounding() -> None:
    # 3 symbols -> 33.3333%, 66.6667%, 100.0%
    vals = {"S1": 1.0, "S2": 2.0, "S3": 3.0}
    ranks = compute_percentile_ranks(vals)
    assert ranks["S1"] == 33.3333
    assert ranks["S2"] == 66.6667
    assert ranks["S3"] == 100.0
