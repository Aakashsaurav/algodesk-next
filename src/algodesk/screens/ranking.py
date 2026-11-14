"""Cross-sectional ranking for screen engine."""

from __future__ import annotations

from collections.abc import Mapping

from algodesk.api.screens import ScreenRow
from algodesk.screens.models import RankSpec


def compute_percentile_ranks(values: Mapping[str, float]) -> dict[str, float]:
    """Compute percentile rank for each symbol across the evaluated universe.

    Our definition:
        percentile = (count of symbols with value <= current symbol value) / total_symbols * 100.0
    """
    total = len(values)
    if total == 0:
        return {}

    all_vals = list(values.values())
    ranks: dict[str, float] = {}

    for sym, val in values.items():
        count_le = sum(1 for v in all_vals if v <= val)
        ranks[sym] = round((count_le / total) * 100.0, 4)

    return ranks


def apply_ranking(
    rows: list[ScreenRow],
    rank_spec: RankSpec,
    metric_values: Mapping[str, float],
) -> list[ScreenRow]:
    """Assign percentile score to rows and filter out rows below rank_spec.min."""
    ranks = compute_percentile_ranks(metric_values)
    updated_rows: list[ScreenRow] = []

    for row in rows:
        pct = ranks.get(row.symbol, 0.0)
        passed = row.passed
        if rank_spec.min is not None and pct < rank_spec.min:
            passed = False

        new_values = dict(row.values)
        new_values["rank_percentile"] = pct
        # Also store the raw rank metric value
        if row.symbol in metric_values:
            new_values["rank_metric"] = metric_values[row.symbol]

        updated_rows.append(
            ScreenRow(
                symbol=row.symbol,
                passed=passed,
                score=pct,
                values=new_values,
            )
        )

    return updated_rows
