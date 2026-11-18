"""AlgoDesk ops inspect helper for fee schedules."""

from __future__ import annotations

from datetime import date
from typing import Any

from algodesk.api.errors import ConfigError
from algodesk.api.kinds import Mode
from algodesk.fees.engine import _coerce_schedule
from algodesk.fees.errors import FeeScheduleError
from algodesk.fees.models import Schedule
from algodesk.ops.context import build_context
from algodesk.ops.runtime import Runtime


def inspect_fee_schedule(
    rt: Runtime,
    schedule: str,
    as_of: str | None = None,
    segment: str | None = None,
    product: str | None = None,
) -> dict[str, Any]:
    """Inspect and resolve applicable fee schedule components for an as-of date."""
    p_ctx = build_context(Mode.RESEARCH, run_id=f"fees_show_{schedule}")
    try:
        fees_plugin: Any = rt.plugins.create("fees", schedule, config={}, ctx=p_ctx)
        sch: Schedule = _coerce_schedule(fees_plugin.schedule())
    except ConfigError:
        raise
    except Exception as e:
        raise ConfigError(
            what=f"Fee schedule plugin '{schedule}' not found or invalid: {e}",
            where=f"fees.show:{schedule}",
            fix="Verify plugins/fees/<schedule>/schedule.toml exists",
        ) from e

    if as_of is not None:
        try:
            eval_date = date.fromisoformat(as_of.strip())
        except Exception as e:
            raise ConfigError(
                what=f"Invalid date format for --as-of: '{as_of}'",
                where=f"fees.show:{schedule}",
                fix="Specify date in ISO format YYYY-MM-DD",
            ) from e
    else:
        eval_date = date.today()

    seg_filter = segment.lower() if segment else None
    prod_filter = product.upper() if product else None

    all_comps = sch.components
    if seg_filter:
        all_comps = [c for c in all_comps if c.segment.lower() == seg_filter]
    if prod_filter:
        all_comps = [c for c in all_comps if c.product.upper() == prod_filter]

    if not all_comps:
        raise ConfigError(
            what=(
                f"No components in schedule '{schedule}' match "
                f"segment={segment!r}, product={product!r}"
            ),
            where=f"fees.show:{schedule}",
            fix="Check available segments and products in schedule.toml",
        )

    # Fail closed if eval_date < earliest verified effective_from
    grouped: dict[tuple[str, str, str | None, str], list[Any]] = {}
    for c in all_comps:
        k = (c.segment.lower(), c.product.upper(), getattr(c, "instrument_type", None), c.name)
        grouped.setdefault(k, []).append(c)

    for (seg, prod, itype, comp_name), comps in sorted(grouped.items()):
        earliest_dt = min(c.effective_from for c in comps)
        if eval_date < earliest_dt:
            itype_str = f" {itype}" if itype else ""
            raise FeeScheduleError(
                what=(
                    f"Trade date {eval_date.isoformat()} is before earliest verified "
                    f"effective_from ({earliest_dt.isoformat()}) for component '{comp_name}' "
                    f"in {seg}:{prod}{itype_str}"
                ),
                where=f"fees.show:{schedule}:{seg}:{prod}:{comp_name}",
                fix=(
                    f"Specify date on or after {earliest_dt.isoformat()} or "
                    "configure fees.fallback = 'earliest' in run config"
                ),
            )

    active_comps = [
        c
        for c in all_comps
        if c.effective_from <= eval_date and (c.effective_to is None or c.effective_to >= eval_date)
    ]

    if not active_comps:
        raise FeeScheduleError(
            what=(
                f"No active fee components found for date {eval_date.isoformat()} "
                f"in schedule '{schedule}'"
            ),
            where=f"fees.show:{schedule}",
            fix="Specify a date where rates are defined in schedule.toml",
        )

    active_taxes = [
        t
        for t in sch.taxes
        if (t.effective_from is None or t.effective_from <= eval_date)
        and (t.effective_to is None or t.effective_to >= eval_date)
    ]

    return {
        "schedule": schedule,
        "as_of": eval_date,
        "meta": sch.meta,
        "active_comps": active_comps,
        "active_taxes": active_taxes,
    }
