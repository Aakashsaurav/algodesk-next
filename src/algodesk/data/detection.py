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


@dataclass(frozen=True, slots=True)
class PriceVerificationResult:
    """Outcome of price-based corporate action verification on raw closes."""

    date: date
    symbol: str
    event_type: str
    declared_factor: float
    observed_factor: float
    verdict: str  # "confirmed" | "conflict" | "suspect_event"
    details: str
    action_id: str | None = None


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


STANDARD_RATIOS: tuple[float, ...] = (
    0.1,  # 10:1 split
    0.125,  # 8:1 split
    1.0 / 7.0,  # 7:1 split
    1.0 / 6.0,  # 5:1 bonus
    0.2,  # 5:1 split or 4:1 bonus
    0.25,  # 4:1 split or 3:1 bonus
    1.0 / 3.0,  # 3:1 split or 2:1 bonus
    0.4,  # 5:2 split
    0.5,  # 2:1 split or 1:1 bonus
    2.0 / 3.0,  # 1:2 bonus
    0.75,  # 1:3 bonus
    0.8,  # 1:4 bonus
)


def _calc_expected_factor(action: ActionRecord) -> float:
    t = action.type.lower()
    if t == "split":
        if action.ratio_old is not None and action.ratio_new is not None:
            return float(action.ratio_new) / float(action.ratio_old)
        if action.ratio is not None and float(action.ratio) > 0.0:
            return 1.0 / float(action.ratio)
    elif t == "bonus":
        if action.ratio_new is not None and action.ratio_old is not None:
            return float(action.ratio_old) / float(action.ratio_new + action.ratio_old)
        if action.ratio is not None and float(action.ratio) > 0.0:
            r_val = float(action.ratio)
            share_mult = r_val + 1.0 if r_val <= 1.0 else r_val
            return 1.0 / share_mult
    return 1.0


def verify_prices_for_actions(
    symbol: str,
    bars: Sequence[Bar],
    actions: Sequence[ActionRecord],
    tolerance_pct: float = 8.0,
    jump_pct: float = 20.0,
) -> list[PriceVerificationResult]:
    """Verify declared splits and bonuses against raw closes, and flag unexplained jumps."""
    if len(bars) < 2:
        return []
    sorted_bars = sorted(bars, key=lambda b: b.start_ts)
    date_to_idx = {b.start_ts.date(): i for i, b in enumerate(sorted_bars)}
    clean_sym = symbol.split(":")[-1].strip().upper()
    results: list[PriceVerificationResult] = []
    action_dates: set[date] = set()

    for a in actions:
        t = a.type.lower()
        if t not in ("split", "bonus"):
            continue
        action_dates.add(a.ex_date)
        idx = date_to_idx.get(a.ex_date)
        if idx is None or idx == 0:
            continue
        curr_b, prev_b = sorted_bars[idx], sorted_bars[idx - 1]
        if curr_b.close <= 0 or prev_b.close <= 0:
            continue
        obs_factor = curr_b.close / prev_b.close
        dec_factor = _calc_expected_factor(a)
        diff_dec = abs(obs_factor - dec_factor) / dec_factor * 100.0
        nearest_std = min(STANDARD_RATIOS, key=lambda s: abs(s - obs_factor))
        diff_std = abs(obs_factor - nearest_std) / nearest_std * 100.0
        std_matches_dec = abs(dec_factor - nearest_std) / nearest_std * 100.0 <= 1.0

        if diff_dec <= tolerance_pct or (std_matches_dec and diff_std <= tolerance_pct):
            verdict = "confirmed"
            eff_diff = min(diff_dec, diff_std) if std_matches_dec else diff_dec
            msg = (
                f"Observed price factor {obs_factor:.4f} matches declared {dec_factor:.4f} "
                f"within {tolerance_pct:.1f}% (diff {eff_diff:.2f}%)"
            )
        else:
            verdict = "conflict"
            msg = (
                f"Observed price factor {obs_factor:.4f} deviates from declared {dec_factor:.4f} "
                f"by {diff_dec:.2f}% (> {tolerance_pct:.1f}%)"
            )
        results.append(
            PriceVerificationResult(
                date=a.ex_date,
                symbol=clean_sym,
                event_type=t,
                declared_factor=dec_factor,
                observed_factor=obs_factor,
                verdict=verdict,
                details=msg,
                action_id=a.id,
            )
        )

    for i in range(1, len(sorted_bars)):
        prev_b, curr_b = sorted_bars[i - 1], sorted_bars[i]
        c_date = curr_b.start_ts.date()
        if prev_b.close <= 0:
            continue
        j_diff = abs(curr_b.close / prev_b.close - 1.0) * 100.0
        if j_diff > jump_pct and not _trading_day_set(sorted_bars, i).intersection(action_dates):
            results.append(
                PriceVerificationResult(
                    date=c_date,
                    symbol=clean_sym,
                    event_type="jump",
                    declared_factor=1.0,
                    observed_factor=curr_b.close / prev_b.close,
                    verdict="suspect_event",
                    details=(
                        f"Unexplained price jump of {j_diff:.1f}% "
                        f"({prev_b.close:.2f} -> {curr_b.close:.2f}) with no corporate action"
                    ),
                )
            )
    return sorted(results, key=lambda r: r.date)
