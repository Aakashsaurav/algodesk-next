"""Fee calculation engine and fill charge processing."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from algodesk.api.errors import ConfigError
from algodesk.api.market import parse_id
from algodesk.api.trading import Charges, Fill, Product, Side
from algodesk.fees.errors import FeeScheduleError
from algodesk.fees.mapping import resolve_fee_segment_and_product
from algodesk.fees.models import Schedule


def _round_half_up(val: Decimal) -> Decimal:
    """Round a Decimal value to 2 decimal places using ROUND_HALF_UP."""
    return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _round_component(val: Decimal, rounding: str | None = None) -> Decimal:
    """Round a component charge according to its schedule rounding rule.

    Default is standard currency rounding to 2 decimal places (ROUND_HALF_UP).
    If rounding is whole rupee half up, rounds to nearest whole rupee, half up.
    """
    if rounding in (
        "whole_rupee_half_up",
        "nearest_whole_rupee_half_up",
        "whole_rupee",
        "round_half_up_rupee",
    ):
        return val.quantize(Decimal("1"), rounding=ROUND_HALF_UP).quantize(Decimal("0.01"))
    return _round_half_up(val)


class ChargesList(list[Charges]):
    """A list of Charges for individual fills with aggregate convenience properties."""

    def __init__(self, *args: Any, fallback_used_count: int = 0, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fallback_used_count = fallback_used_count

    @property
    def total(self) -> Decimal:
        return sum((c.total for c in self), Decimal("0.00"))

    @property
    def items(self) -> dict[str, Decimal]:
        agg: dict[str, Decimal] = {}
        for c in self:
            for k, v in c.items.items():
                agg[k] = agg.get(k, Decimal("0.00")) + v
        return agg

    @property
    def brokerage(self) -> Decimal:
        return sum((c.brokerage for c in self), Decimal("0.00"))

    @property
    def stt(self) -> Decimal:
        return sum((c.stt for c in self), Decimal("0.00"))

    @property
    def exchange_txn(self) -> Decimal:
        return sum((c.exchange_txn for c in self), Decimal("0.00"))

    @property
    def sebi(self) -> Decimal:
        return sum((c.sebi for c in self), Decimal("0.00"))

    @property
    def stamp(self) -> Decimal:
        return sum((c.stamp for c in self), Decimal("0.00"))

    @property
    def dp(self) -> Decimal:
        return sum((c.dp for c in self), Decimal("0.00"))

    @property
    def gst(self) -> Decimal:
        return sum((c.gst for c in self), Decimal("0.00"))

    def combined(self) -> Charges:
        return Charges(items=self.items, total=self.total)


def _coerce_schedule(schedule: Any) -> Schedule:
    """Coerce various representations (Schedule, ScheduleData, dict) to a Schedule model."""
    if isinstance(schedule, Schedule):
        return schedule
    if hasattr(schedule, "raw") and isinstance(schedule.raw, dict):
        return Schedule.model_validate(schedule.raw)
    if isinstance(schedule, dict):
        return Schedule.model_validate(schedule)
    raise TypeError(f"Cannot coerce {type(schedule).__name__} to Schedule")


def _component_matches(
    c: Any,
    seg: str,
    prod: str,
    dt: date,
    exch: str,
    itype: str | None,
) -> bool:
    if c.segment.lower() != seg or c.product.upper() != prod:
        return False
    if c.effective_from > dt or (c.effective_to is not None and c.effective_to < dt):
        return False
    c_exch = getattr(c, "exchange", None)
    if c_exch is not None and c_exch.lower() != exch:
        return False
    c_itype = getattr(c, "instrument_type", None)
    if itype is None:
        if c_itype is not None:
            return False
    elif itype == "fut":
        if c_itype not in (None, "fut"):
            return False
    elif itype == "opt":
        if c_itype != "opt":
            return False
    return True


def _compute_component_raw(c: Any, turnover: Decimal, is_first: bool) -> Decimal:
    if c.kind == "flat":
        return c.amount or Decimal("0.00")
    if c.kind == "pct":
        return turnover * (c.rate or Decimal("0.00"))
    if c.kind == "pct_capped":
        raw = turnover * (c.rate or Decimal("0.00"))
        if c.cap is not None:
            raw = min(raw, c.cap)
        if c.minimum is not None:
            raw = max(raw, c.minimum)
        return raw
    if c.kind == "per_crore":
        crores = turnover / Decimal("10000000")
        return crores * (c.rate or c.amount or Decimal("0.00"))
    if c.kind == "per_scrip_day":
        return (c.amount or Decimal("0.00")) if is_first else Decimal("0.00")
    return Decimal("0.00")


def _calculate_taxes(
    taxes: Iterable[Any],
    components: Mapping[str, Decimal],
    trade_date: date,
    fallback_mode: str | None = None,
) -> tuple[dict[str, Decimal], bool]:
    tax_items: dict[str, Decimal] = {}
    tax_fb = False
    for tax in taxes:
        if tax.effective_from and trade_date < tax.effective_from:
            if fallback_mode == "earliest":
                tax_fb = True
            else:
                raise FeeScheduleError(
                    what=(
                        f"Trade date {trade_date.isoformat()} is before earliest verified "
                        f"effective_from ({tax.effective_from.isoformat()}) for tax '{tax.name}'"
                    ),
                    where=f"FeeEngine:tax:{tax.name}",
                    fix=(
                        "Configure fees.fallback = 'earliest' in run config to extrapolate "
                        f"earliest verified rates or trade on or after "
                        f"{tax.effective_from.isoformat()}"
                    ),
                )
        if tax.effective_to and trade_date > tax.effective_to:
            continue
        taxable_base = sum(components.get(k, Decimal("0.00")) for k in tax.applies_to)
        tax_items[tax.name] = tax_items.get(tax.name, Decimal("0.00")) + _round_half_up(
            taxable_base * tax.rate
        )
    return tax_items, tax_fb


def _resolve_fill_attributes(
    fill: Fill,
    orders: Mapping[str, Any] | None,
) -> tuple[Side, Product, str, str, str, str | None]:
    """Resolve side, product, segment, symbol, exchange, instrument_type for a fill."""
    side = fill.side
    product = fill.product
    symbol = "DEFAULT"

    if orders and fill.client_ref in orders:
        ord_info = orders[fill.client_ref]
        if side is None and hasattr(ord_info, "side"):
            side = ord_info.side
        elif side is None and hasattr(ord_info, "spec"):
            side = ord_info.spec.side
        if product is None and hasattr(ord_info, "product"):
            product = ord_info.product
        elif product is None and hasattr(ord_info, "spec"):
            product = ord_info.spec.product

    segment, prod_str, exchange, itype = resolve_fee_segment_and_product(fill.instrument, product)
    if fill.instrument:
        try:
            parts = parse_id(fill.instrument)
            symbol = parts.symbol.upper()
        except Exception:
            symbol = str(fill.instrument).upper()

    final_prod = product if product is not None else getattr(Product, prod_str, Product.CNC)
    return (
        side if side is not None else Side.BUY,
        final_prod,
        segment,
        symbol,
        exchange,
        itype,
    )


class FeeEngine:
    """Pure fee calculation engine driving schedules against trades and fills."""

    fallback: str | None = None
    fallback_used_count: int = 0

    def __init__(self, fallback: str | None = None) -> None:
        self.fallback = fallback if fallback is not None else FeeEngine.fallback
        self.fallback_used = 0

    @classmethod
    def set_fallback(cls, fallback: str | None) -> None:
        """Set default fallback mode for fee calculation (e.g. 'earliest')."""
        cls.fallback = fallback

    @classmethod
    def reset_fallback_count(cls) -> None:
        """Reset the count of fallback uses."""
        cls.fallback_used_count = 0

    @classmethod
    def get_fallback_used(cls) -> int:
        """Return total times fallback was used across calculations."""
        return cls.fallback_used_count

    @classmethod
    def calculate_trade_charges(
        cls,
        turnover: Decimal,
        side: Side | str,
        segment: str,
        product: Product | str,
        trade_date: date,
        schedule: Schedule | Any,
        *,
        exchange: str = "nse",
        instrument_type: str | None = None,
        instrument: Any = None,
        is_exercise: bool = False,
        is_first_scrip_day: bool = True,
        fallback: str | None = None,
    ) -> Charges:
        """Calculate itemised charges and total for a single trade execution."""
        if is_exercise:
            from algodesk.api.errors import NotSupported

            raise NotSupported(
                what="Options exercise pricing is not supported",
                where="FeeEngine.calculate_trade_charges",
                fix=(
                    "Exercise settlement pricing requires option intrinsic pricing "
                    "which is not implemented"
                ),
            )

        sch = _coerce_schedule(schedule)
        side_str = (side.value if hasattr(side, "value") else str(side)).lower()

        if instrument is not None:
            seg_str, prod_str, exch_str, itype = resolve_fee_segment_and_product(
                instrument, product, default_exchange=exchange
            )
        else:
            seg_candidate = segment.lower()
            if seg_candidate in ("fut", "opt"):
                seg_str = "fno"
                itype = instrument_type.lower() if instrument_type else seg_candidate
            else:
                seg_str = seg_candidate
                itype = (
                    instrument_type.lower()
                    if instrument_type
                    else ("fut" if seg_str == "fno" else None)
                )
            prod_str = (product.value if hasattr(product, "value") else str(product)).upper()
            exch_str = exchange.lower()

        fallback_mode = fallback or cls.fallback

        # If MCX instrument traded and no MCX schedule components exist, charges are not modelled
        if exch_str == "mcx" and not any(
            getattr(c, "exchange", None) == "mcx" for c in sch.components
        ):
            return Charges(
                total=Decimal("0.00"),
                items={},
            )

        # Filter candidate components for segment/product/exchange/instrument_type
        relevant = [
            c
            for c in sch.components
            if c.segment.lower() == seg_str
            and c.product.upper() == prod_str
            and (c.exchange is None or c.exchange.lower() == exch_str)
            and (
                itype is None
                and c.instrument_type is None
                or itype == "fut"
                and c.instrument_type in (None, "fut")
                or itype == "opt"
                and c.instrument_type == "opt"
            )
        ]
        if not relevant:
            raise ConfigError(
                f"No fee schedule components found for segment={segment!r}, "
                f"product={product!r}, date={trade_date.isoformat()}",
                where=f"FeeEngine:{segment}:{product}",
                fix=(
                    "Add matching component rates in schedule.toml for this segment, "
                    "product and date range"
                ),
            )

        # Collect distinct components applicable to this trade's side
        side_applicable = [c for c in relevant if c.side == "both" or c.side.lower() == side_str]
        comp_groups: dict[str, list[Any]] = {}
        for c in side_applicable:
            comp_groups.setdefault(c.name, []).append(c)

        matching: list[Any] = []
        fallback_used_here = False

        for comp_name, comps in comp_groups.items():
            earliest_dt = min(c.effective_from for c in comps)
            if trade_date < earliest_dt:
                if fallback_mode == "earliest":
                    earliest_comp = min(comps, key=lambda c: c.effective_from)
                    matching.append(earliest_comp)
                    fallback_used_here = True
                else:
                    raise FeeScheduleError(
                        what=(
                            f"Trade date {trade_date.isoformat()} is before earliest verified "
                            f"effective_from ({earliest_dt.isoformat()}) "
                            f"for component '{comp_name}' ({segment}:{product})"
                        ),
                        where=f"FeeEngine:{segment}:{product}:{comp_name}",
                        fix=(
                            "Configure fees.fallback = 'earliest' in run config to extrapolate "
                            f"earliest verified rates or trade on or after "
                            f"{earliest_dt.isoformat()}"
                        ),
                    )
            else:
                matched_c = [
                    c
                    for c in comps
                    if _component_matches(c, seg_str, prod_str, trade_date, exch_str, itype)
                ]
                if not matched_c:
                    if fallback_mode == "earliest":
                        earliest_comp = min(comps, key=lambda c: c.effective_from)
                        matching.append(earliest_comp)
                        fallback_used_here = True
                    else:
                        raise FeeScheduleError(
                            what=(
                                f"No verified rate found for component '{comp_name}' "
                                f"on trade date {trade_date.isoformat()}"
                            ),
                            where=f"FeeEngine:{segment}:{product}:{comp_name}",
                            fix=(
                                "Add component rate in schedule.toml or configure "
                                "fees.fallback = 'earliest'"
                            ),
                        )
                else:
                    matching.extend(matched_c)

        items: dict[str, Decimal] = {}
        for c in matching:
            raw = _compute_component_raw(c, turnover, is_first_scrip_day)
            rounding_rule = getattr(c, "rounding", None)
            items[c.name] = items.get(c.name, Decimal("0.00")) + _round_component(
                raw, rounding_rule
            )

        tax_items, tax_fb = _calculate_taxes(
            sch.taxes, items, trade_date, fallback_mode=fallback_mode
        )
        if tax_fb:
            fallback_used_here = True

        if fallback_used_here:
            cls.fallback_used_count += 1

        all_items: dict[str, Decimal] = {**items, **tax_items}
        return Charges(items=all_items, total=sum(all_items.values(), Decimal("0.00")))

    @classmethod
    def charges_for_fills(
        cls,
        fills: Iterable[Fill],
        schedule: Schedule | Any,
        *,
        orders: Mapping[str, Any] | None = None,
        fallback: str | None = None,
    ) -> ChargesList:
        """Compute charges for a sequence of fills, applying DP charges once per scrip/day."""
        sch = _coerce_schedule(schedule)
        before_count = cls.fallback_used_count
        results = ChargesList()
        seen_scrip_days: set[tuple[date, str]] = set()

        for fill in fills:
            turnover = Decimal(fill.qty) * fill.price
            trade_date = fill.ts.date()
            side, product, segment, symbol, exch, itype = _resolve_fill_attributes(fill, orders)

            scrip_key = (trade_date, symbol)
            is_first = scrip_key not in seen_scrip_days
            if is_first:
                seen_scrip_days.add(scrip_key)

            chg = cls.calculate_trade_charges(
                turnover=turnover,
                side=side,
                segment=segment,
                product=product,
                trade_date=trade_date,
                schedule=sch,
                exchange=exch,
                instrument_type=itype,
                is_first_scrip_day=is_first,
                fallback=fallback,
            )
            results.append(chg)

        results.fallback_used_count = cls.fallback_used_count - before_count
        return results


def charges_for_fills(
    fills: Iterable[Fill],
    schedule: Schedule,
    *,
    orders: Mapping[str, Any] | None = None,
    fallback: str | None = None,
) -> ChargesList:
    """Convenience functional wrapper for FeeEngine.charges_for_fills."""
    return FeeEngine.charges_for_fills(fills, schedule, orders=orders, fallback=fallback)
