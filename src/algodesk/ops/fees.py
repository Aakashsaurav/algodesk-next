"""AlgoDesk ops inspect helper for fee schedules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from algodesk.api.errors import ConfigError
from algodesk.api.kinds import Mode
from algodesk.api.market import parse_id
from algodesk.config.models import RunConfig
from algodesk.data.aliases import resolve_alias
from algodesk.fees.engine import _coerce_schedule
from algodesk.fees.errors import FeeScheduleError
from algodesk.fees.models import Schedule
from algodesk.ops.context import build_context
from algodesk.ops.runtime import Runtime


@dataclass
class FeeComponentStatus:
    """Readiness status for a single fee component required by a backtest run."""

    name: str
    segment: str
    product: str
    exchange: str
    instrument_type: str | None
    earliest_verified: date
    status: str  # "OK", "FALLBACK", "ERROR"
    fix: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "segment": self.segment,
            "product": self.product,
            "exchange": self.exchange,
            "instrument_type": self.instrument_type,
            "earliest_verified": self.earliest_verified.isoformat(),
            "status": self.status,
            "fix": self.fix,
        }


@dataclass
class FeeReadinessReport:
    """Targeted fee readiness report evaluating only components used by backtest."""

    schedule: str
    run_start: date
    fallback_configured: bool
    components: list[FeeComponentStatus] = field(default_factory=list)
    has_errors: bool = False
    error_message: str = ""
    remediation_fix: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schedule": self.schedule,
            "run_start": self.run_start.isoformat(),
            "fallback_configured": self.fallback_configured,
            "has_errors": self.has_errors,
            "components": [c.to_dict() for c in self.components],
        }


def check_fees_readiness(
    run_config: RunConfig,
    runtime: Runtime,
    start_override: date | None = None,
    manifest: Any = None,
) -> FeeReadinessReport:
    """Verify that fee rates are verified as-of the backtest start date for needed components."""
    bt_cfg = run_config.backtest
    assert bt_cfg is not None

    if start_override is not None:
        run_start = start_override
    elif isinstance(bt_cfg.start, date):
        run_start = bt_cfg.start
    else:
        run_start = date.fromisoformat(str(bt_cfg.start))

    sch_name = run_config.fees.schedule if run_config.fees else "zerodha_nse"
    fallback_cfg = run_config.fees is not None and run_config.fees.fallback == "earliest"
    if not fallback_cfg and runtime.params is not None:
        try:
            fallback_cfg = runtime.params.get("fees.fallback") == "earliest"
        except Exception:
            pass

    p_ctx = build_context(Mode.RESEARCH, run_id=f"fees_check_{sch_name}")
    try:
        fees_plugin: Any = runtime.plugins.create("fees", sch_name, config={}, ctx=p_ctx)
        sch: Schedule = _coerce_schedule(fees_plugin.schedule())
    except Exception as e:
        raise ConfigError(
            what=f"Fee schedule plugin '{sch_name}' not found or invalid: {e}",
            where=f"fees.check:{sch_name}",
            fix="Verify plugins/fees/<schedule>/schedule.toml exists",
        ) from e

    report = FeeReadinessReport(
        schedule=sch_name,
        run_start=run_start,
        fallback_configured=fallback_cfg,
    )
    needed = _resolve_needed_fee_components(run_config, runtime, manifest=manifest)
    _evaluate_components_readiness(
        report, sch, needed, run_start, fallback_cfg, run_config.run.name
    )
    return report


def _resolve_needed_fee_components(
    run_config: RunConfig,
    runtime: Runtime,
    manifest: Any = None,
) -> set[tuple[str, str, str, str | None]]:
    """Determine distinct (segment, product, exchange, instrument_type) required by the run."""
    from algodesk.ops.readiness import resolve_universe_for_backtest

    members, _, _, _, _, _ = resolve_universe_for_backtest(run_config, runtime, manifest=manifest)
    products: set[str] = set()
    for st in run_config.strategies:
        p_val = st.params.get("product") if st.params else None
        products.add(str(p_val).upper() if p_val else "CNC")
    if not products:
        products.add("CNC")

    needed: set[tuple[str, str, str, str | None]] = set()
    for sym in members:
        c_inst = resolve_alias(sym, manifest=manifest)
        try:
            parts = parse_id(c_inst)
            exch = parts.exchange.lower()
            seg = parts.segment.lower()
            itype = None
            if seg == "fno":
                itype = "opt" if ("CE" in parts.symbol or "PE" in parts.symbol) else "fut"
        except Exception:
            exch, seg, itype = "nse", "eq", None

        for prod in products:
            needed.add((seg, prod, exch, itype))
    return needed


def _evaluate_components_readiness(
    report: FeeReadinessReport,
    sch: Schedule,
    needed: set[tuple[str, str, str, str | None]],
    run_start: date,
    fallback_cfg: bool,
    run_name: str,
) -> None:
    """Evaluate each required component and applicable taxes against schedule effective dates."""
    for seg, prod, exch, itype in sorted(needed):
        matched = [
            c
            for c in sch.components
            if c.segment.lower() == seg
            and c.product.upper() == prod
            and (c.exchange is None or c.exchange.lower() == exch)
            and (
                itype is None
                and c.instrument_type is None
                or itype == "fut"
                and c.instrument_type in (None, "fut")
                or itype == "opt"
                and c.instrument_type == "opt"
            )
        ]
        comp_groups: dict[str, list[Any]] = {}
        for c in matched:
            comp_groups.setdefault(c.name, []).append(c)

        for comp_name, comps in sorted(comp_groups.items()):
            earliest_dt = min(c.effective_from for c in comps)
            status, fix = _status_for_date(
                run_start, earliest_dt, fallback_cfg, comp_name, run_name
            )
            if status == "ERROR":
                report.has_errors = True
                if not report.error_message:
                    report.error_message = (
                        f"Backtest start date {run_start.isoformat()} is before earliest verified "
                        f"rate ({earliest_dt.isoformat()}) for required component '{comp_name}' "
                        f"({seg}:{prod})"
                    )
                    report.remediation_fix = fix
            report.components.append(
                FeeComponentStatus(
                    name=comp_name,
                    segment=seg,
                    product=prod,
                    exchange=exch,
                    instrument_type=itype,
                    earliest_verified=earliest_dt,
                    status=status,
                    fix=fix,
                )
            )

    for tax in sch.taxes:
        if tax.effective_from is not None:
            status, fix = _status_for_date(
                run_start, tax.effective_from, fallback_cfg, tax.name, run_name
            )
            if status == "ERROR":
                report.has_errors = True
                if not report.error_message:
                    report.error_message = (
                        f"Backtest start date {run_start.isoformat()} is before earliest verified "
                        f"tax rate ({tax.effective_from.isoformat()}) for '{tax.name}'"
                    )
                    report.remediation_fix = fix
            report.components.append(
                FeeComponentStatus(
                    name=tax.name,
                    segment="tax",
                    product="ALL",
                    exchange="nse",
                    instrument_type=None,
                    earliest_verified=tax.effective_from,
                    status=status,
                    fix=fix,
                )
            )


def _status_for_date(
    run_start: date,
    earliest_dt: date,
    fallback_cfg: bool,
    comp_name: str,
    run_name: str,
) -> tuple[str, str]:
    if run_start >= earliest_dt:
        return "OK", ""
    if fallback_cfg:
        return "FALLBACK", ""
    fix = (
        f"Specify backtest start on or after {earliest_dt.isoformat()} or "
        f"configure fees.fallback = 'earliest' in config/runs/{run_name}.toml"
    )
    return "ERROR", fix


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
        # Future-dated unrequested segments (e.g. options from 2026) or pre-start DP
        # do not block viewing active equity fees
        if not seg_filter and itype == "opt":
            continue
        if not seg_filter and comp_name == "dp" and eval_date < earliest_dt:
            continue
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
