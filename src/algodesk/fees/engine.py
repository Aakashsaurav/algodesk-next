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
        is_first_scrip_day: bool = True,
    ) -> Charges:
        """Calculate itemised charges and total for a single trade execution."""
        sch = _coerce_schedule(schedule)
        side_str = (side.value if hasattr(side, "value") else str(side)).lower()
        prod_str = (product.value if hasattr(product, "value") else str(product)).upper()
        seg_str = segment.lower()

        # Check if schedule defines components for this segment, product and date
        matching_components = [
            c
            for c in sch.components
            if c.segment.lower() == seg_str
            and c.product.upper() == prod_str
            and c.effective_from <= trade_date
            and (c.effective_to is None or c.effective_to >= trade_date)
        ]

        if not matching_components:
            raise ConfigError(
                f"No fee schedule components found for segment={segment!r}, "
                f"product={product!r}, date={trade_date.isoformat()}",
                where=f"FeeEngine:{segment}:{product}",
                fix=(
                    "Add matching component rates in schedule.toml for this segment, "
                    "product and date range"
                ),
            )

        component_items: dict[str, Decimal] = {}

        for c in matching_components:
            if c.side != "both" and c.side.lower() != side_str:
                continue

            raw: Decimal = Decimal("0.00")
            if c.kind == "flat":
                raw = c.amount or Decimal("0.00")
            elif c.kind == "pct":
                raw = turnover * (c.rate or Decimal("0.00"))
            elif c.kind == "pct_capped":
                raw = turnover * (c.rate or Decimal("0.00"))
                if c.cap is not None:
                    raw = min(raw, c.cap)
                if c.minimum is not None:
                    raw = max(raw, c.minimum)
            elif c.kind == "per_crore":
                crores = turnover / Decimal("10000000")
                raw = crores * (c.rate or c.amount or Decimal("0.00"))
            elif c.kind == "per_scrip_day":
                if is_first_scrip_day:
                    raw = c.amount or Decimal("0.00")
                else:
                    raw = Decimal("0.00")

            rounded = _round_half_up(raw)
            component_items[c.name] = component_items.get(c.name, Decimal("0.00")) + rounded

        # Calculate taxes
        tax_items: dict[str, Decimal] = {}
        for tax in sch.taxes:
            if tax.effective_from and trade_date < tax.effective_from:
                continue
            if tax.effective_to and trade_date > tax.effective_to:
                continue

            taxable_base = sum(
                component_items.get(c_name, Decimal("0.00")) for c_name in tax.applies_to
            )
            tax_raw = taxable_base * tax.rate
            tax_items[tax.name] = tax_items.get(tax.name, Decimal("0.00")) + _round_half_up(tax_raw)

        all_items: dict[str, Decimal] = {**component_items, **tax_items}
        total = sum(all_items.values(), Decimal("0.00"))
        return Charges(items=all_items, total=total)

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

            # Resolve side, product, segment, and symbol
            side = fill.side
            product = fill.product
            segment: str = "eq"
            symbol: str = "DEFAULT"

            if fill.instrument:
                try:
                    parts = parse_id(fill.instrument)
                    segment = parts.segment.lower()
                    symbol = parts.symbol.upper()
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

            if side is None:
                side = Side.BUY
            if product is None:
                product = Product.CNC

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
