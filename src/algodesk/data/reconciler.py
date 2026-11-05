"""Corporate actions reconciler comparing feeds, checking tolerances, and applying overrides."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from algodesk.api.market import ActionRecord
from algodesk.data.overrides import ActionOverride, load_overrides, override_to_action


def _calc_event_factor(a: ActionRecord) -> float:
    t = a.type.lower()
    if t == "split":
        if a.ratio_new is not None and a.ratio_old is not None and a.ratio_old > 0:
            return float(a.ratio_new) / float(a.ratio_old)
        return float(a.ratio or 1.0)
    if t == "bonus":
        if a.ratio_new is not None and a.ratio_old is not None and a.ratio_old > 0:
            return float(a.ratio_new + a.ratio_old) / float(a.ratio_old)
        r = float(a.ratio) if a.ratio is not None else 1.0
        return r + 1.0 if r <= 1.0 else r
    if t == "rights":
        if a.issue_price is not None and (a.ratio is not None or a.ratio_new is not None):
            return 1.0
    return 1.0


class CorporateActionsReconciler:
    """Reconciles multi-source corporate actions with overrides and tolerance checks."""

    __slots__ = ("_dividend_tolerance_pct", "_overrides_path", "_primary_source")

    def __init__(
        self,
        primary_source: str = "upstox",
        dividend_tolerance_pct: float = 1.0,
        overrides_path: Path | str | None = None,
    ) -> None:
        self._primary_source = primary_source.lower()
        self._dividend_tolerance_pct = dividend_tolerance_pct
        self._overrides_path = overrides_path

    def _select_action(
        self,
        actions: list[ActionRecord],
        status: str,
        conflict_reason: str = "",
    ) -> ActionRecord:
        c = next((a for a in actions if a.source.lower() == self._primary_source), actions[0])
        ratio = c.ratio
        ratio_old = c.ratio_old
        ratio_new = c.ratio_new
        if ratio is None and c.type.lower() in ("split", "bonus"):
            for other in actions:
                if other.ratio is not None:
                    ratio = other.ratio
                    ratio_old = other.ratio_old
                    ratio_new = other.ratio_new
                    break

        amount = c.amount
        if amount is None and c.type.lower() == "dividend":
            for other in actions:
                if other.amount is not None:
                    amount = other.amount
                    break

        return ActionRecord(
            ex_date=c.ex_date,
            type=c.type,
            ratio=ratio,
            ratio_old=ratio_old,
            ratio_new=ratio_new,
            amount=amount,
            record_date=c.record_date,
            text=c.text,
            source=c.source,
            source_ref=c.source_ref,
            fetched_at=c.fetched_at,
            symbol=c.symbol,
            exchange=c.exchange,
            status=status,
            id=c.id,
            issue_price=c.issue_price,
            reason=conflict_reason or ("ratio disagreement" if status == "conflict" else c.reason),
            verified_on=c.verified_on,
        )

    def _reconcile_split_bonus(self, acts: list[ActionRecord]) -> str:
        ratios = [a.ratio for a in acts if a.ratio is not None]
        if not ratios:
            return "manual_required"
        r0 = ratios[0]
        return (
            "conflict"
            if any(abs(r - r0) / max(abs(r0), 1e-6) > 1e-3 for r in ratios[1:])
            else "confirmed"
        )

    def _reconcile_dividend(self, acts: list[ActionRecord]) -> str:
        amounts = [a.amount for a in acts if a.amount is not None]
        if len(amounts) < len(acts):
            return "manual_required"
        a0 = float(amounts[0])
        tol = self._dividend_tolerance_pct
        diff = any(abs(float(a) - a0) / max(a0, 1e-6) * 100.0 > tol for a in amounts[1:])
        return "conflict" if diff else "confirmed"

    def get_split_bonus_factors(self, actions: Sequence[ActionRecord]) -> dict[date, float]:
        """Compute date -> factor mapping for all split, bonus, and rights events."""
        by_date: dict[date, float] = {}
        for a in sorted(actions, key=lambda x: x.ex_date):
            if a.type.lower() in ("split", "bonus", "rights"):
                f = _calc_event_factor(a)
                if a.ex_date not in by_date or f > by_date[a.ex_date]:
                    by_date[a.ex_date] = f
        return by_date

    def cumulative_factor_after(
        self, split_bonus_by_date: dict[date, float], target_date: date
    ) -> float:
        """Compute cumulative multiplier of later split and bonus events after target_date."""
        cum = 1.0
        for d, f in split_bonus_by_date.items():
            if d > target_date:
                cum *= f
        return cum

    def _check_implied_split(
        self, event: ActionRecord, all_actions: Sequence[ActionRecord]
    ) -> tuple[bool, str]:
        """Check whether yfinance dividends before and after differ by the event factor."""
        divs = [a for a in all_actions if a.type.lower() == "dividend" and a.amount is not None]
        by_d: dict[date, dict[str, float]] = defaultdict(dict)
        for d in divs:
            by_d[d.ex_date][d.source.lower()] = float(d.amount)  # type: ignore[arg-type]

        scale_by_date: dict[date, float] = {}
        for ex_d, s_map in by_d.items():
            if "yfinance" in s_map:
                dec_val = s_map.get("nse") or s_map.get("upstox")
                if dec_val is not None:
                    scale_by_date[ex_d] = dec_val / max(s_map["yfinance"], 1e-6)

        before_dates = [d for d in scale_by_date if d < event.ex_date]
        after_dates = [d for d in scale_by_date if d > event.ex_date]
        if not before_dates or not after_dates:
            return False, ""

        d_before = max(before_dates)
        d_after = min(after_dates)
        s_before = scale_by_date[d_before]
        s_after = scale_by_date[d_after]
        implied_factor = s_before / max(s_after, 1e-6)
        event_factor = _calc_event_factor(event)

        diff_pct = abs(implied_factor - event_factor) / max(event_factor, 1e-6) * 100.0
        if diff_pct <= self._dividend_tolerance_pct:
            return True, f"dividends before and after differ by factor {event_factor:g}"
        return False, ""

    def _reconcile_same_date(
        self,
        acts: list[ActionRecord],
        later_factor: float,
        all_actions: Sequence[ActionRecord],
    ) -> ActionRecord:
        types = {a.type.lower() for a in acts}
        sources = {a.source.lower() for a in acts if a.source}

        # Case 1: Single source event — never conflict
        if len(sources) <= 1:
            first = acts[0]
            t = first.type.lower()
            if t in ("split", "bonus"):
                has_implied, implied_desc = self._check_implied_split(first, all_actions)
                if has_implied:
                    reason = f"confirmed (implied): {implied_desc}"
                    if first.source.lower() == "nse":
                        reason += f"; yfinance actions lack the NSE {t} of {first.ex_date}"
                    return self._select_action(
                        acts, status="confirmed (implied)", conflict_reason=reason
                    )
                reason = f"unverified: single-source {t}"
                if first.source.lower() == "nse":
                    reason += f"; yfinance actions lack the NSE {t} of {first.ex_date}"
                return self._select_action(acts, status="unverified", conflict_reason=reason)

            if t == "rights":
                st = "manual_required" if first.issue_price is None else "unverified"
                reason = (
                    "Rights issue price not explicitly stated"
                    if first.issue_price is None
                    else "single-source rights issue"
                )
                return self._select_action(acts, status=st, conflict_reason=reason)

            if t == "other" or first.status == "manual_required":
                return self._select_action(
                    acts,
                    status="manual_required",
                    conflict_reason=first.reason or "Unrecognised corporate action purpose",
                )

            return self._select_action(
                acts, status="unverified", conflict_reason=f"unverified: single-source {t}"
            )

        # Case 2: Multiple sources with matching split and bonus (e.g. NSE bonus and yfinance split)
        if types == {"bonus", "split"}:
            nse_b = next(
                (a for a in acts if a.source.lower() == "nse" and a.type.lower() == "bonus"), None
            )
            yf_s = next(
                (a for a in acts if a.source.lower() == "yfinance" and a.type.lower() == "split"),
                None,
            )
            if nse_b is not None and yf_s is not None:
                f_nse = _calc_event_factor(nse_b)
                f_yf = _calc_event_factor(yf_s)
                if abs(f_nse - f_yf) < 1e-3:
                    has_implied, implied_desc = self._check_implied_split(nse_b, all_actions)
                    st = "confirmed (implied)" if has_implied else "confirmed"
                    lack_msg = f"yfinance actions lack the NSE bonus of {nse_b.ex_date}"
                    if has_implied:
                        reason = f"confirmed (implied): {implied_desc}; {lack_msg}"
                    else:
                        reason = f"confirmed: bonus factor matches split ratio; {lack_msg}"
                    return self._select_action([nse_b, yf_s], status=st, conflict_reason=reason)

        if len(types) > 1:
            return self._select_action(acts, status="conflict", conflict_reason="type mismatch")

        # Case 3: Matching types across sources
        t = acts[0].type.lower()
        if t in ("split", "bonus"):
            st = self._reconcile_split_bonus(acts)
            r = (
                "ratio disagreement"
                if st == "conflict"
                else ("confirmed: values agree across sources" if st == "confirmed" else "")
            )
            return self._select_action(acts, status=st, conflict_reason=r)

        if t == "dividend":
            yf_div = next(
                (a for a in acts if a.source.lower() == "yfinance" and a.amount is not None), None
            )
            dec_divs = [a for a in acts if a.source.lower() != "yfinance" and a.amount is not None]
            if yf_div is not None and dec_divs:
                yf_val = float(yf_div.amount)  # type: ignore[arg-type]
                tol = self._dividend_tolerance_pct
                has_diff = False
                norm_vals = []
                for d in dec_divs:
                    dec_val = float(d.amount)  # type: ignore[arg-type]
                    norm_val = dec_val / later_factor
                    norm_vals.append(norm_val)
                    diff_pct = abs(yf_val - norm_val) / max(norm_val, 1e-6) * 100.0
                    if diff_pct > tol:
                        has_diff = True
                        break
                if has_diff:
                    return self._select_action(
                        acts, status="conflict", conflict_reason="ratio disagreement"
                    )
                d0 = dec_divs[0]
                reason = (
                    f"confirmed: {d0.source.upper()} declared Rs {d0.amount} normalised to "
                    f"Rs {norm_vals[0]:.4g} via later split/bonus factor {later_factor:.4g}"
                    if later_factor != 1.0
                    else "confirmed: direct dividend agreement"
                )
                return self._select_action(acts, status="confirmed", conflict_reason=reason)

            st = self._reconcile_dividend(acts)
            r = (
                "ratio disagreement"
                if st == "conflict"
                else ("confirmed: dividend amounts match" if st == "confirmed" else "")
            )
            return self._select_action(acts, status=st, conflict_reason=r)

        st = "manual_required" if t == "other" else "confirmed"
        return self._select_action(acts, status=st)

    def classify_evidence(self, rec: ActionRecord, matching: Sequence[ActionRecord]) -> str:
        """Classify evidence: direct, normalised, implied, override, single-source, none."""
        if rec.reason and "operator override" in rec.reason.lower():
            return "override"
        if rec.status == "confirmed (implied)":
            return "implied"
        if rec.status == "confirmed":
            if rec.reason and "price" in rec.reason.lower():
                return "price"
            if (
                rec.type.lower() == "dividend"
                and any(a.source.lower() == "yfinance" for a in matching)
                and any(a.source.lower() != "yfinance" for a in matching)
                and "normalised" in (rec.reason or "").lower()
            ):
                return "normalised"
            return "direct"
        if rec.status == "conflict":
            return "none"
        sources = {a.source.lower() for a in matching if a.source}
        if len(sources) <= 1:
            return "single-source"
        return "none"

    def reconcile(
        self,
        exchange: str,
        symbol: str,
        actions: Sequence[ActionRecord],
        overrides: Sequence[ActionOverride] | None = None,
    ) -> list[ActionRecord]:
        """Reconcile action feeds, honoring overrides first and applying status taxonomy."""
        exch, sym = exchange.upper(), symbol.upper()
        all_ovs = list(overrides) if overrides is not None else load_overrides(self._overrides_path)
        matching_ovs = [ov for ov in all_ovs if ov.symbol == sym and ov.exchange == exch]
        ov_dates = {ov.ex_date for ov in matching_ovs}
        remaining = [a for a in actions if a.symbol.upper() == sym and a.ex_date not in ov_dates]
        date_conflicted = self._detect_date_conflicts(remaining)

        split_bonus_by_date = self.get_split_bonus_factors(remaining)

        by_date: dict[date, list[ActionRecord]] = defaultdict(list)
        for a in remaining:
            by_date[a.ex_date].append(a)

        reconciled = [override_to_action(ov) for ov in matching_ovs]
        for ex_d, acts in by_date.items():
            if ex_d in date_conflicted:
                rec = self._select_action(
                    acts, status="conflict", conflict_reason="ex_date disagreement across sources"
                )
            else:
                later_factor = self.cumulative_factor_after(split_bonus_by_date, ex_d)
                rec = self._reconcile_same_date(acts, later_factor, remaining)
            reconciled.append(rec)

        return sorted(reconciled, key=lambda a: (a.ex_date, a.type))

    def _detect_date_conflicts(self, acts: Sequence[ActionRecord]) -> set[date]:
        conflicted: set[date] = set()
        for i, a1 in enumerate(acts):
            for a2 in acts[i + 1 :]:
                if (
                    a1.source.lower() != a2.source.lower()
                    and a1.type == a2.type
                    and a1.ex_date != a2.ex_date
                    and abs((a1.ex_date - a2.ex_date).days) <= 1
                ):
                    conflicted.update((a1.ex_date, a2.ex_date))
        return conflicted
