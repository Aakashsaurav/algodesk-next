"""Price-based corporate action detection on raw 1d candlestick bars."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from algodesk.api.market import ActionRecord, Bar


@dataclass(frozen=True, slots=True)
class DetectionItem:
    """An issue or anomaly detected from price series analysis."""

    symbol: str
    date: date
    kind: str  # "suspect_event" | "conflict"
    details: str


def _trading_day_set(bars: Sequence[Bar], idx: int) -> set[date]:
    indices = [idx] + ([idx - 1] if idx > 0 else []) + ([idx + 1] if idx + 1 < len(bars) else [])
    return {bars[i].start_ts.date() for i in indices}


def detect_price_anomalies(
    symbol: str,
    bars: Sequence[Bar],
    actions: Sequence[ActionRecord],
    detect_jump_pct: float = 20.0,
    ratio_tolerance_pct: float = 10.0,
) -> list[DetectionItem]:
    """Detect suspect price jumps without actions, and declared ratios absent from prices."""
    if len(bars) < 2:
        return []
    sorted_bars = sorted(bars, key=lambda b: b.start_ts)
    items = _detect_suspect_jumps(symbol, sorted_bars, actions, detect_jump_pct)
    items.extend(_detect_absent_ratios(symbol, sorted_bars, actions, ratio_tolerance_pct))
    return sorted(items, key=lambda it: it.date)


def _detect_suspect_jumps(
    symbol: str, bars: list[Bar], actions: Sequence[ActionRecord], detect_jump_pct: float
) -> list[DetectionItem]:
    items: list[DetectionItem] = []
    action_dates = {a.ex_date for a in actions}
    for i in range(1, len(bars)):
        prev_b, curr_b = bars[i - 1], bars[i]
        if prev_b.close <= 0:
            continue
        jump_pct = abs(curr_b.close / prev_b.close - 1.0) * 100.0
        if jump_pct > detect_jump_pct and not _trading_day_set(bars, i).intersection(action_dates):
            items.append(
                DetectionItem(
                    symbol.upper(),
                    curr_b.start_ts.date(),
                    "suspect_event",
                    f"Price jump of {jump_pct:.1f}% ({prev_b.close:.2f} -> {curr_b.close:.2f}) "
                    f"with no corporate action within 1 trading day",
                )
            )
    return items


def _detect_absent_ratios(
    symbol: str, bars: list[Bar], actions: Sequence[ActionRecord], ratio_tolerance_pct: float
) -> list[DetectionItem]:
    items: list[DetectionItem] = []
    date_to_idx = {bars[i].start_ts.date(): i for i in range(len(bars))}
    for a in actions:
        if a.type.lower() not in ("split", "bonus") or a.ratio is None or a.ratio <= 0:
            continue
        idx = date_to_idx.get(a.ex_date, 0)
        if idx == 0:
            continue
        curr_b, prev_b = bars[idx], bars[idx - 1]
        if curr_b.close <= 0 or prev_b.close <= 0:
            continue
        obs_ratio = prev_b.close / curr_b.close
        exp_ratio = float(a.ratio)
        diff_pct = abs(obs_ratio - exp_ratio) / exp_ratio * 100.0
        if diff_pct > ratio_tolerance_pct:
            items.append(
                DetectionItem(
                    symbol.upper(),
                    a.ex_date,
                    "conflict",
                    f"Declared {a.type} ratio {exp_ratio:.4f} absent from prices: "
                    f"observed {obs_ratio:.4f} (diff {diff_pct:.1f}% > {ratio_tolerance_pct}%)",
                )
            )
    return items
