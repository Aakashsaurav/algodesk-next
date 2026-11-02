"""Read-time price and volume adjustment view across all timeframes (M2-4)."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import date, tzinfo
from zoneinfo import ZoneInfo

from algodesk.api.errors import DataError
from algodesk.api.market import ActionRecord, Bar

_DEFAULT_EXCH_TZ = ZoneInfo("Asia/Kolkata")


def _calc_split_ratio(action: ActionRecord) -> float:
    if action.ratio_old is not None and action.ratio_new is not None:
        return float(action.ratio_old) / float(action.ratio_new)
    if action.ratio is not None and float(action.ratio) > 0.0:
        return float(action.ratio)
    raise DataError(
        what=f"Split action on {action.ex_date} for {action.symbol} lacks valid ratio",
        where="AdjustmentView._calc_split_ratio",
        fix="Provide ratio or ratio_old/ratio_new via override",
    )


def _calc_bonus_ratio(action: ActionRecord) -> float:
    if action.ratio_new is not None and action.ratio_old is not None:
        return float(action.ratio_new + action.ratio_old) / float(action.ratio_old)
    if action.ratio is not None and float(action.ratio) > 0.0:
        r_val = float(action.ratio)
        return r_val + 1.0 if r_val <= 1.0 else r_val
    raise DataError(
        what=f"Bonus action on {action.ex_date} for {action.symbol} lacks valid ratio",
        where="AdjustmentView._calc_bonus_ratio",
        fix="Provide ratio_new and ratio_old (x:y) via override",
    )


def _find_prev_close(
    ex_date: date,
    bars: Sequence[Bar],
    daily_bars: Sequence[Bar] | None,
    exchange_tz: tzinfo,
) -> float | None:
    ref = daily_bars if daily_bars is not None else bars
    candidates = [b.close for b in ref if b.start_ts.astimezone(exchange_tz).date() < ex_date]
    return candidates[-1] if candidates else None


def _calc_dividend_factor(
    action: ActionRecord,
    bars: Sequence[Bar],
    daily_bars: Sequence[Bar] | None,
    exchange_tz: tzinfo,
) -> float:
    p = _find_prev_close(action.ex_date, bars, daily_bars, exchange_tz)
    if p is None or p <= 0.0:
        return 1.0
    amt = float(action.amount) if action.amount is not None else 0.0
    if amt <= 0.0:
        return 1.0
    f = 1.0 - (amt / p)
    if f <= 0.0:
        raise DataError(
            what=f"Dividend {amt} exceeds prior close {p} on {action.ex_date} for {action.symbol}",
            where="AdjustmentView._calc_dividend_factor",
            fix="Check dividend amount or prior prices",
        )
    return f


def _calc_rights_factor(
    action: ActionRecord,
    bars: Sequence[Bar],
    daily_bars: Sequence[Bar] | None,
    exchange_tz: tzinfo,
) -> float:
    if action.issue_price is None or (action.ratio is None and action.ratio_new is None):
        raise DataError(
            what=f"Rights issue on {action.ex_date} for {action.symbol} requires price and ratio",
            where="AdjustmentView._calc_rights_factor",
            fix="Add operator override with --issue-price and --ratio",
        )
    p = _find_prev_close(action.ex_date, bars, daily_bars, exchange_tz)
    if p is None or p <= 0.0:
        return 1.0
    p_issue = float(action.issue_price)
    if action.ratio_new is not None and action.ratio_old is not None:
        r = float(action.ratio_new) / float(action.ratio_old)
    else:
        r = float(action.ratio or 0.0)
    terp = (p + r * p_issue) / (1.0 + r)
    return terp / p


def _filter_and_validate_actions(
    actions: Sequence[ActionRecord],
    mode: str,
    accept_unverified: bool,
) -> list[ActionRecord]:
    filtered: list[ActionRecord] = []
    for a in actions:
        sym = a.symbol or "UNKNOWN"
        d_iso = a.ex_date.isoformat()
        if a.status in ("conflict", "manual_required"):
            msg = f"Action {a.type} on {d_iso} for {sym} status '{a.status}': {a.reason or a.text}"
            raise DataError(
                what=msg,
                where="AdjustmentView",
                fix=f"Resolve with 'algodesk data actions override add --symbol {sym} ...'",
            )
        t = a.type.lower()
        active = (mode == "splits" and t in ("split", "bonus")) or (
            mode == "total" and t in ("split", "bonus", "dividend", "rights")
        )
        if active:
            if a.status == "unverified" and not accept_unverified:
                raise DataError(
                    what=f"Action {a.type} on {d_iso} for {sym} is unverified",
                    where="AdjustmentView",
                    fix="Pass accept_unverified=True or add operator override",
                )
            filtered.append(a)
    return filtered


def _compute_action_factors(
    actions: Sequence[ActionRecord],
    bars: Sequence[Bar],
    mode: str,
    daily_bars: Sequence[Bar] | None,
    exchange_tz: tzinfo,
    accept_unverified: bool,
) -> tuple[list[tuple[date, float, float, str]], list[str], str]:
    if mode == "none" or not actions:
        return [], [], ""

    filtered = _filter_and_validate_actions(actions, mode, accept_unverified)
    factors: list[tuple[date, float, float, str]] = []
    applied_ids: list[str] = []
    hash_parts: list[str] = []

    for a in sorted(filtered, key=lambda x: (x.ex_date, x.id)):
        t = a.type.lower()
        if t == "split":
            r = _calc_split_ratio(a)
            f_price, f_vol = 1.0 / r, r
        elif t == "bonus":
            r = _calc_bonus_ratio(a)
            f_price, f_vol = 1.0 / r, r
        elif t == "dividend":
            f_price, f_vol = _calc_dividend_factor(a, bars, daily_bars, exchange_tz), 1.0
        elif t == "rights":
            f_price, f_vol = _calc_rights_factor(a, bars, daily_bars, exchange_tz), 1.0
        else:
            continue

        factors.append((a.ex_date, f_price, f_vol, a.id))
        applied_ids.append(a.id)
        hash_parts.append(f"{a.id}:{f_price:.8f}:{f_vol:.4f}")

    h = hashlib.sha256(",".join(hash_parts).encode()).hexdigest() if hash_parts else ""
    return factors, applied_ids, h


def _adjust_single_bar(
    b: Bar,
    factors: list[tuple[date, float, float, str]],
    tz: tzinfo,
) -> Bar:
    b_date = b.start_ts.astimezone(tz).date()
    f_p, v_d = 1.0, 1.0
    for ex_d, fp, vd, _ in factors:
        if ex_d > b_date:
            f_p *= fp
            v_d *= vd

    if f_p == 1.0 and v_d == 1.0:
        return b

    adj_open, adj_high = b.open * f_p, b.high * f_p
    adj_low, adj_close = b.low * f_p, b.close * f_p
    p_all = (adj_open, adj_high, adj_low, adj_close)
    true_low, true_high = min(p_all), max(p_all)
    adj_open = min(max(adj_open, true_low), true_high)
    adj_close = min(max(adj_close, true_low), true_high)
    adj_vol = int(round(b.volume / v_d)) if v_d > 0.0 else b.volume

    return Bar(
        b.instrument,
        b.timeframe,
        b.start_ts,
        b.end_ts,
        adj_open,
        true_high,
        true_low,
        adj_close,
        adj_vol,
        b.complete,
        b.synthetic,
    )


class AdjustmentView:
    """Read-time price and volume adjustment view across all timeframes."""

    @classmethod
    def apply(
        cls,
        bars: Sequence[Bar],
        actions: Sequence[ActionRecord],
        mode: str = "total",
        daily_bars: Sequence[Bar] | None = None,
        exchange_tz: tzinfo | None = None,
        accept_unverified: bool = False,
    ) -> tuple[tuple[Bar, ...], list[str], str]:
        """Apply corporate action adjustments backwards to bars."""
        if not bars or mode == "none" or not actions:
            return tuple(bars), [], ""

        tz = exchange_tz or _DEFAULT_EXCH_TZ
        factors, action_ids, h = _compute_action_factors(
            actions=actions,
            bars=bars,
            mode=mode,
            daily_bars=daily_bars,
            exchange_tz=tz,
            accept_unverified=accept_unverified,
        )
        if not factors:
            return tuple(bars), [], ""

        adjusted = [_adjust_single_bar(b, factors, tz) for b in bars]
        return tuple(adjusted), action_ids, h
