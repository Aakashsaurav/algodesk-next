"""Corporate actions reconciler comparing feeds, checking tolerances, and applying overrides."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from algodesk.api.market import ActionRecord
from algodesk.data.overrides import ActionOverride, load_overrides, override_to_action


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
        return ActionRecord(
            ex_date=c.ex_date,
            type=c.type,
            ratio=c.ratio,
            ratio_old=c.ratio_old,
            ratio_new=c.ratio_new,
            amount=c.amount,
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
            reason=conflict_reason or c.reason,
            verified_on=c.verified_on,
        )

    def _reconcile_split_bonus(self, acts: list[ActionRecord]) -> str:
        ratios = [a.ratio for a in acts if a.ratio is not None]
        if len(ratios) < len(acts):
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

    def _reconcile_same_date(self, acts: list[ActionRecord]) -> ActionRecord:
        if len({a.source.lower() for a in acts if a.source}) <= 1:
            st = (
                "manual_required"
                if acts[0].type == "other" or acts[0].status == "manual_required"
                else "unverified"
            )
            return self._select_action(acts, status=st)

        if len({a.type.lower() for a in acts}) > 1:
            return self._select_action(acts, status="conflict", conflict_reason="type mismatch")

        t = acts[0].type.lower()
        if t in ("split", "bonus"):
            st = self._reconcile_split_bonus(acts)
        elif t == "dividend":
            st = self._reconcile_dividend(acts)
        else:
            st = "manual_required" if t == "other" else "confirmed"

        return self._select_action(acts, status=st)

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
                rec = self._reconcile_same_date(acts)
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
