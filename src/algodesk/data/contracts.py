"""Derivative contract resolution for futures and options.

Resolves near/next future contracts, option contracts by expiry/strike/type,
respecting contract.rollover_days and calendar holiday adjustments.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from algodesk.api.errors import DataError
from algodesk.api.market import Instrument


class ContractResolver:
    """Generic strategy-independent derivative contract resolver."""

    def __init__(
        self,
        instruments: Sequence[Instrument],
        calendar: Any | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> None:
        self._instruments = list(instruments)
        self._calendar = calendar
        self._params = dict(params) if params else {}

    def _get_rollover_days(self) -> int:
        val = self._params.get("contract.rollover_days", 3)
        try:
            return int(val)
        except (ValueError, TypeError):
            return 3

    def _effective_expiry(self, exp: date) -> date:
        """Adjust contract expiry to preceding trading day if holiday/weekend.

        Fails closed if calendar cannot determine session coverage.
        """
        if self._calendar is None:
            return exp

        # Check if is_session / is_trading_day exists
        check_fn = getattr(
            self._calendar,
            "is_session",
            getattr(self._calendar, "is_trading_day", None),
        )
        if check_fn is None:
            return exp

        try:
            # Check if exp is trading day
            if check_fn(exp):
                return exp

            # Preceding trading day
            curr = exp - timedelta(days=1)
            for _ in range(10):  # max 10 days back for consecutive holidays
                if check_fn(curr):
                    return curr
                curr -= timedelta(days=1)

            raise DataError(
                what=f"Cannot determine trading day prior to holiday expiry {exp.isoformat()}",
                where="ContractResolver._effective_expiry",
                fix="Verify calendar data coverage",
            )
        except Exception as e:
            if isinstance(e, DataError):
                raise
            raise DataError(
                what=f"Calendar coverage check failed for expiry {exp.isoformat()}: {e}",
                where="ContractResolver._effective_expiry",
                fix="Ensure calendar data covers the contract series",
            ) from e

    def resolve_contract(
        self,
        underlying: str,
        kind: str,
        rule: str | Mapping[str, Any],
        as_of: date,
    ) -> Instrument:
        """Resolve generic contract for given underlying, kind, rule, and evaluation date."""
        und_upper = underlying.strip().upper()
        norm_kind = "FUT" if kind.lower() in ("future", "fut") else "OPT"

        # Pool matching candidates
        candidates: list[tuple[date, Instrument]] = []
        for inst in self._instruments:
            inst_und = (inst.underlying or inst.symbol).strip().upper()
            if inst_und != und_upper:
                continue
            if inst.segment != norm_kind:
                continue
            if inst.expiry is None:
                continue

            eff_exp = self._effective_expiry(inst.expiry)
            if eff_exp >= as_of:
                candidates.append((eff_exp, inst))

        if not candidates:
            raise DataError(
                what=(
                    f"No active {norm_kind} contracts found for "
                    f"'{und_upper}' as of {as_of.isoformat()}"
                ),
                where="ContractResolver.resolve_contract",
                fix="Ensure instrument master contains active contracts for this underlying",
            )

        # Sort by adjusted expiry ascending
        candidates.sort(key=lambda x: x[0])

        if norm_kind == "FUT":
            rule_str = (rule if isinstance(rule, str) else str(rule.get("series", "near"))).lower()
            rollover_days = self._get_rollover_days()

            # Group candidates by unique expiry
            unique_expiries: list[tuple[date, list[Instrument]]] = []
            for eff_exp, inst in candidates:
                if not unique_expiries or unique_expiries[-1][0] != eff_exp:
                    unique_expiries.append((eff_exp, [inst]))
                else:
                    unique_expiries[-1][1].append(inst)

            near_exp, near_insts = unique_expiries[0]
            days_to_near = (near_exp - as_of).days

            if rule_str == "near":
                if days_to_near <= rollover_days and len(unique_expiries) > 1:
                    # Roll over to next contract
                    return unique_expiries[1][1][0]
                return near_insts[0]

            if rule_str == "next":
                if len(unique_expiries) < 2:
                    raise DataError(
                        what=(
                            f"Next future requested for '{und_upper}' as of {as_of} "
                            "but only one active expiry found"
                        ),
                        where="ContractResolver.resolve_contract",
                        fix="Select 'near' series or supply additional forward contracts",
                    )
                return unique_expiries[1][1][0]

            raise DataError(
                what=f"Unsupported future rule '{rule_str}'. Expected 'near' or 'next'",
                where="ContractResolver.resolve_contract",
                fix="Specify 'near' or 'next'",
            )

        # OPT
        if not isinstance(rule, Mapping):
            raise DataError(
                what=(
                    "Option contract resolution requires rule dictionary specifying "
                    "expiry, strike, and type"
                ),
                where="ContractResolver.resolve_contract",
                fix="Pass rule dict e.g. {'strike': Decimal('24000'), 'type': 'CE'}",
            )

        req_type = str(rule.get("type", "")).upper()
        if req_type not in ("CE", "PE"):
            raise DataError(
                what=f"Option rule must specify type 'CE' or 'PE', got '{req_type}'",
                where="ContractResolver.resolve_contract",
                fix="Set type to 'CE' or 'PE'",
            )

        req_strike = rule.get("strike")
        if req_strike is None:
            raise DataError(
                what="Option rule must specify strike price",
                where="ContractResolver.resolve_contract",
                fix="Provide strike price Decimal in rule dict",
            )
        target_strike = Decimal(str(req_strike))

        req_exp = rule.get("expiry")
        target_exp: date | None = None
        if isinstance(req_exp, date):
            target_exp = req_exp
        elif isinstance(req_exp, str) and req_exp.lower() != "near":
            target_exp = date.fromisoformat(req_exp)

        # Filter option candidates
        matching_opts = []
        for eff_exp, inst in candidates:
            if inst.option_type != req_type:
                continue
            if inst.strike != target_strike:
                continue
            if target_exp is not None:
                if eff_exp != target_exp and inst.expiry != target_exp:
                    continue
            matching_opts.append(inst)

        if not matching_opts:
            raise DataError(
                what=(
                    f"No matching option found for '{und_upper}' with strike "
                    f"{target_strike} and type {req_type} as of {as_of.isoformat()}"
                ),
                where="ContractResolver.resolve_contract",
                fix="Verify available option strikes and expiries in master",
            )

        return matching_opts[0]
