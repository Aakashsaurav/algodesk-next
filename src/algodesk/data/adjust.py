"""Read-time price and volume adjustment view across all timeframes (M2-4)."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import date, tzinfo
from zoneinfo import ZoneInfo

from algodesk.api.errors import DataError
from algodesk.api.market import ActionRecord, Bar

_DEFAULT_EXCH_TZ = ZoneInfo("Asia/Kolkata")
VALID_BASES = {"none", "splits", "total"}


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


class AdjustmentResult(tuple):  # type: ignore[type-arg]
    """Result of adjustment view: unpacks as (bars, action_ids, hash) with metadata properties."""

    bars: tuple[Bar, ...]
    action_ids: list[str]
    hash: str
    factor_kinds: list[str]
    basis: str
    from_basis: str

    def __new__(
        cls,
        bars: tuple[Bar, ...] | Sequence[Bar],
        action_ids: Sequence[str] = (),
        hash_str: str = "",
        factor_kinds: Sequence[str] = (),
        basis: str = "",
        from_basis: str = "",
    ) -> AdjustmentResult:
        t_bars = tuple(bars)
        l_ids = list(action_ids)
        instance = super().__new__(cls, (t_bars, l_ids, hash_str))
        instance.bars = t_bars
        instance.action_ids = l_ids
        instance.hash = hash_str
        instance.factor_kinds = sorted(set(factor_kinds))
        instance.basis = basis
        instance.from_basis = from_basis
        return instance


class AdjustmentView:
    """Read-time price and volume adjustment view across all timeframes."""

    @classmethod
    def apply(
        cls,
        bars: Sequence[Bar],
        actions: Sequence[ActionRecord],
        mode: str | None = None,
        from_basis: str | None = None,
        to_basis: str | None = None,
        daily_bars: Sequence[Bar] | None = None,
        exchange_tz: tzinfo | None = None,
        accept_unverified: bool = False,
    ) -> AdjustmentResult:
        """Apply corporate action adjustments backwards to bars based on basis transition."""
        if from_basis is None:
            if mode is not None and to_basis is None:
                src_basis = "none"
                tgt_basis = mode
            else:
                src_basis = "splits"
                tgt_basis = to_basis or mode or "total"
        else:
            src_basis = from_basis.lower()
            tgt_basis = (to_basis or mode or "total").lower()

        if src_basis not in VALID_BASES:
            raise DataError(
                what=f"Invalid source adjustment basis '{src_basis}'",
                where="AdjustmentView.apply",
                fix=f"Source basis must be one of {sorted(VALID_BASES)}",
            )
        if tgt_basis not in VALID_BASES:
            raise DataError(
                what=f"Invalid target adjustment basis '{tgt_basis}'",
                where="AdjustmentView.apply",
                fix=f"Target basis must be one of {sorted(VALID_BASES)}",
            )

        if not bars:
            return AdjustmentResult(
                (), [], "", factor_kinds=[], basis=tgt_basis, from_basis=src_basis
            )

        # 1. Any conflict or manual_required action must refuse
        for a in actions:
            if a.status in ("conflict", "manual_required"):
                d_iso = a.ex_date.isoformat()
                sym = a.symbol or "UNKNOWN"
                r_msg = a.reason or a.text
                raise DataError(
                    what=f"Action {a.type} on {d_iso} for {sym} status '{a.status}': {r_msg}",
                    where="AdjustmentView",
                    fix=f"Resolve with 'algodesk data actions override add --symbol {sym} ...'",
                )

        # 2. Same basis: unchanged
        if src_basis == tgt_basis:
            return AdjustmentResult(
                tuple(bars), [], "", factor_kinds=[], basis=tgt_basis, from_basis=src_basis
            )

        # 2. total -> anything else: refuse with clear error
        if src_basis == "total":
            raise DataError(
                what=f"Cannot convert '{src_basis}' adjusted series to '{tgt_basis}' basis",
                where="AdjustmentView.apply",
                fix=(
                    "Total adjusted series cannot be converted to splits or none "
                    "without unadjusted prices"
                ),
            )

        tz = exchange_tz or _DEFAULT_EXCH_TZ
        factors: list[tuple[date, float, float, str]] = []
        applied_ids: list[str] = []
        hash_parts: list[str] = []
        factor_kinds: list[str] = []

        # 3. splits -> total: applies dividend factors only
        if src_basis == "splits" and tgt_basis == "total":
            for a in sorted(actions, key=lambda x: (x.ex_date, x.id)):
                t = a.type.lower()
                if t not in ("dividend", "rights"):
                    continue
                d_iso = a.ex_date.isoformat()
                sym = a.symbol or "UNKNOWN"
                if a.status in ("conflict", "manual_required"):
                    r_msg = a.reason or a.text
                    raise DataError(
                        what=f"Action {a.type} on {d_iso} for {sym} status '{a.status}': {r_msg}",
                        where="AdjustmentView",
                        fix=f"Resolve with 'algodesk data actions override add --symbol {sym} ...'",
                    )
                if a.status == "unverified" and not accept_unverified:
                    raise DataError(
                        what=f"Action {a.type} on {d_iso} for {sym} is unverified",
                        where="AdjustmentView",
                        fix="Pass accept_unverified=True or add operator override",
                    )
                if t == "dividend":
                    f_price = _calc_dividend_factor(a, bars, daily_bars, tz)
                    f_vol = 1.0
                    factor_kinds.append("dividend")
                else:
                    f_price = _calc_rights_factor(a, bars, daily_bars, tz)
                    f_vol = 1.0
                    factor_kinds.append("rights")

                factors.append((a.ex_date, f_price, f_vol, a.id))
                applied_ids.append(a.id)
                hash_parts.append(f"{a.id}:{f_price:.8f}:{f_vol:.4f}")

        # 4. splits -> none: reverses split and bonus factors
        elif src_basis == "splits" and tgt_basis == "none":
            # Refuse if any split or bonus is unverified, conflict or manual_required
            for a in actions:
                t = a.type.lower()
                if t in ("split", "bonus"):
                    if a.status in ("unverified", "conflict", "manual_required"):
                        d_iso = a.ex_date.isoformat()
                        sym = a.symbol or "UNKNOWN"
                        raise DataError(
                            what=(
                                f"Cannot reverse splits: {a.type} on {d_iso} for {sym} "
                                f"has status '{a.status}'"
                            ),
                            where="AdjustmentView.apply",
                            fix=(
                                "All split and bonus actions must be confirmed "
                                "before reversing to none basis"
                            ),
                        )

            for a in sorted(actions, key=lambda x: (x.ex_date, x.id)):
                t = a.type.lower()
                if t == "split":
                    r = _calc_split_ratio(a)
                    f_price, f_vol = r, 1.0 / r
                    factor_kinds.append("split")
                elif t == "bonus":
                    r = _calc_bonus_ratio(a)
                    f_price, f_vol = r, 1.0 / r
                    factor_kinds.append("bonus")
                else:
                    continue

                factors.append((a.ex_date, f_price, f_vol, a.id))
                applied_ids.append(a.id)
                hash_parts.append(f"{a.id}:{f_price:.8f}:{f_vol:.4f}")

        # 5. none -> splits: applies splits
        # 6. none -> total: applies both splits and dividends/rights
        elif src_basis == "none":
            allowed_types = (
                {"split", "bonus"}
                if tgt_basis == "splits"
                else {"split", "bonus", "dividend", "rights"}
            )
            for a in sorted(actions, key=lambda x: (x.ex_date, x.id)):
                t = a.type.lower()
                if t not in allowed_types:
                    continue
                d_iso = a.ex_date.isoformat()
                sym = a.symbol or "UNKNOWN"
                if a.status in ("conflict", "manual_required"):
                    r_msg = a.reason or a.text
                    raise DataError(
                        what=f"Action {a.type} on {d_iso} for {sym} status '{a.status}': {r_msg}",
                        where="AdjustmentView",
                        fix=f"Resolve with 'algodesk data actions override add --symbol {sym} ...'",
                    )
                if a.status == "unverified" and not accept_unverified:
                    raise DataError(
                        what=f"Action {a.type} on {d_iso} for {sym} is unverified",
                        where="AdjustmentView",
                        fix="Pass accept_unverified=True or add operator override",
                    )
                if t == "split":
                    r = _calc_split_ratio(a)
                    f_price, f_vol = 1.0 / r, r
                    factor_kinds.append("split")
                elif t == "bonus":
                    r = _calc_bonus_ratio(a)
                    f_price, f_vol = 1.0 / r, r
                    factor_kinds.append("bonus")
                elif t == "dividend":
                    f_price = _calc_dividend_factor(a, bars, daily_bars, tz)
                    f_vol = 1.0
                    factor_kinds.append("dividend")
                elif t == "rights":
                    f_price = _calc_rights_factor(a, bars, daily_bars, tz)
                    f_vol = 1.0
                    factor_kinds.append("rights")
                else:
                    continue

                factors.append((a.ex_date, f_price, f_vol, a.id))
                applied_ids.append(a.id)
                hash_parts.append(f"{a.id}:{f_price:.8f}:{f_vol:.4f}")

        h = hashlib.sha256(",".join(hash_parts).encode()).hexdigest() if hash_parts else ""
        if not factors:
            return AdjustmentResult(
                tuple(bars), applied_ids, h, factor_kinds, basis=tgt_basis, from_basis=src_basis
            )

        adjusted = [_adjust_single_bar(b, factors, tz) for b in bars]
        return AdjustmentResult(
            tuple(adjusted), applied_ids, h, factor_kinds, basis=tgt_basis, from_basis=src_basis
        )
