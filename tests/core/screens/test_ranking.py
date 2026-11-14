"""Tests for cross-sectional percentile ranking."""

from __future__ import annotations

from algodesk.api.screens import ScreenRow
from algodesk.screens.models import OperandModel, RankSpec
from algodesk.screens.ranking import apply_ranking, compute_percentile_ranks


def test_percentile_empty() -> None:
    ranks = compute_percentile_ranks({})
    assert ranks == {}


def test_percentile_single_symbol() -> None:
    ranks = compute_percentile_ranks({"S1": 100.0})
    assert ranks == {"S1": 100.0}


def test_percentile_distinct_five_symbols() -> None:
    values = {"S1": 10.0, "S2": 20.0, "S3": 30.0, "S4": 40.0, "S5": 50.0}
    ranks = compute_percentile_ranks(values)
    assert ranks["S1"] == 20.0
    assert ranks["S2"] == 40.0
    assert ranks["S3"] == 60.0
    assert ranks["S4"] == 80.0
    assert ranks["S5"] == 100.0


def test_percentile_with_ties() -> None:
    # 4 symbols: [10, 20, 20, 30]
    # S1 (10): 1/4 -> 25.0%
    # S2 (20): 3/4 -> 75.0%
    # S3 (20): 3/4 -> 75.0%
    # S4 (30): 4/4 -> 100.0%
    values = {"S1": 10.0, "S2": 20.0, "S3": 20.0, "S4": 30.0}
    ranks = compute_percentile_ranks(values)
    assert ranks["S1"] == 25.0
    assert ranks["S2"] == 75.0
    assert ranks["S3"] == 75.0
    assert ranks["S4"] == 100.0


def test_percentile_all_identical() -> None:
    values = {"S1": 50.0, "S2": 50.0, "S3": 50.0}
    ranks = compute_percentile_ranks(values)
    assert ranks["S1"] == 100.0
    assert ranks["S2"] == 100.0
    assert ranks["S3"] == 100.0


def test_apply_ranking_without_min() -> None:
    rows = [
        ScreenRow(symbol="S1", passed=True, score=100.0),
        ScreenRow(symbol="S2", passed=True, score=100.0),
        ScreenRow(symbol="S3", passed=False, score=0.0),
    ]
    spec = RankSpec(by=OperandModel(field="close"))
    metric_vals = {"S1": 10.0, "S2": 30.0, "S3": 20.0}
    updated = apply_ranking(rows, spec, metric_vals)

    by_sym = {r.symbol: r for r in updated}
    assert by_sym["S1"].score == round(1 / 3 * 100, 4)
    assert by_sym["S1"].passed is True
    assert by_sym["S2"].score == 100.0
    assert by_sym["S2"].passed is True
    assert by_sym["S3"].score == round(2 / 3 * 100, 4)
    assert by_sym["S3"].passed is False  # stayed False


def test_apply_ranking_with_min_filters_out_lower_percentiles() -> None:
    rows = [
        ScreenRow(symbol="S1", passed=True),
        ScreenRow(symbol="S2", passed=True),
        ScreenRow(symbol="S3", passed=True),
        ScreenRow(symbol="S4", passed=True),
        ScreenRow(symbol="S5", passed=True),
    ]
    # Percentiles: S1=20%, S2=40%, S3=60%, S4=80%, S5=100%
    spec = RankSpec(by=OperandModel(field="close"), min=70.0)
    metric_vals = {"S1": 10.0, "S2": 20.0, "S3": 30.0, "S4": 40.0, "S5": 50.0}
    updated = apply_ranking(rows, spec, metric_vals)

    by_sym = {r.symbol: r for r in updated}
    assert by_sym["S1"].passed is False  # 20 < 70
    assert by_sym["S2"].passed is False  # 40 < 70
    assert by_sym["S3"].passed is False  # 60 < 70
    assert by_sym["S4"].passed is True  # 80 >= 70
    assert by_sym["S5"].passed is True  # 100 >= 70


def test_apply_ranking_stores_metric_in_values() -> None:
    rows = [ScreenRow(symbol="S1", passed=True)]
    spec = RankSpec(by=OperandModel(field="close"))
    metric_vals = {"S1": 123.45}
    updated = apply_ranking(rows, spec, metric_vals)
    assert updated[0].values["rank_metric"] == 123.45
    assert updated[0].values["rank_percentile"] == 100.0
