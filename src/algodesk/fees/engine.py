"""Fee calculation engine and fill charge processing."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from algodesk.api.errors import ConfigError
from algodesk.api.market import parse_id
from algodesk.api.trading import Charges, Fill, Product, Side
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
) -> dict[str, Decimal]:
    tax_items: dict[str, Decimal] = {}
    for tax in taxes:
        if tax.effective_from and trade_date < tax.effective_from:
            continue
        if tax.effective_to and trade_date > tax.effective_to:
            continue
        taxable_base = sum(components.get(k, Decimal("0.00")) for k in tax.applies_to)
        tax_items[tax.name] = tax_items.get(tax.name, Decimal("0.00")) + _round_half_up(
            taxable_base * tax.rate
        )
    return tax_items


def _resolve_fill_attributes(
    fill: Fill,
    orders: Mapping[str, Any] | None,
) -> tuple[Side, Product, str, str, str, str | None]:
    """Resolve side, product, segment, symbol, exchange, instrument_type for a fill."""
    side = fill.side
    product = fill.product
    segment = "eq"
    symbol = "DEFAULT"
    exchange = "nse"
    itype: str | None = None

    if fill.instrument:
        try:
            parts = parse_id(fill.instrument)
            exchange = parts.exchange.lower()
            segment = parts.segment.lower()
            symbol = parts.symbol.upper()
            if segment == "fno":
                if "FUT" in symbol:
                    itype = "fut"
                elif "CE" in symbol or "PE" in symbol:
                    itype = "opt"
                else:
                    itype = "fut"
        except Exception:
            symbol = str(fill.instrument).upper()

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

    return (
        side if side is not None else Side.BUY,
        product if product is not None else Product.CNC,
        segment,
        symbol,
        exchange,
        itype,
    )


class FeeEngine:
    """Pure fee calculation engine driving schedules against trades and fills."""

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
        is_exercise: bool = False,
        is_first_scrip_day: bool = True,
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
        prod_str = (product.value if hasattr(product, "value") else str(product)).upper()
        seg_str = segment.lower()
        exch_str = exchange.lower()
        itype = (
            instrument_type.lower() if instrument_type else ("fut" if seg_str == "fno" else None)
        )

        matching = [
            c
            for c in sch.components
            if _component_matches(c, seg_str, prod_str, trade_date, exch_str, itype)
        ]
        if not matching:
            raise ConfigError(
                f"No fee schedule components found for segment={segment!r}, "
                f"product={product!r}, date={trade_date.isoformat()}",
                where=f"FeeEngine:{segment}:{product}",
                fix=(
                    "Add matching component rates in schedule.toml for this segment, "
                    "product and date range"
                ),
            )

        items: dict[str, Decimal] = {}
        for c in matching:
            if c.side != "both" and c.side.lower() != side_str:
                continue
            raw = _compute_component_raw(c, turnover, is_first_scrip_day)
            rounding_rule = getattr(c, "rounding", None)
            items[c.name] = items.get(c.name, Decimal("0.00")) + _round_component(
                raw, rounding_rule
            )

        tax_items = _calculate_taxes(sch.taxes, items, trade_date)
        all_items: dict[str, Decimal] = {**items, **tax_items}
        return Charges(items=all_items, total=sum(all_items.values(), Decimal("0.00")))

    @classmethod
    def charges_for_fills(
        cls,
        fills: Iterable[Fill],
        schedule: Schedule | Any,
        *,
        orders: Mapping[str, Any] | None = None,
    ) -> ChargesList:
        """Compute charges for a sequence of fills, applying DP charges once per scrip/day."""
        sch = _coerce_schedule(schedule)
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
            )
            results.append(chg)

        return results


def charges_for_fills(
    fills: Iterable[Fill],
    schedule: Schedule,
    *,
    orders: Mapping[str, Any] | None = None,
) -> ChargesList:
    """Convenience functional wrapper for FeeEngine.charges_for_fills."""
    return FeeEngine.charges_for_fills(fills, schedule, orders=orders)
