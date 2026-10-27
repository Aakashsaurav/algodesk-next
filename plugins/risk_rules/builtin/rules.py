"""Nine builtin pre-trade risk rules.

Imports only algodesk.api. All thresholds resolved dynamically from parameters.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from algodesk.api import Intent, OrderType, PlaceOrder, RiskDecision, Side


def _get_float_param(view: Any, name: str) -> float | None:
    profile = getattr(view, "profile", "conservative")
    param_key = f"risk.{profile}.{name}"
    params = getattr(view, "params", None)
    if params is not None and hasattr(params, "get"):
        try:
            pval = params.get(param_key)
            if pval is not None:
                return float(getattr(pval, "value", pval))
        except Exception:
            return None
    return None


def _get_int_param(view: Any, name: str) -> int | None:
    profile = getattr(view, "profile", "conservative")
    param_key = f"risk.{profile}.{name}"
    params = getattr(view, "params", None)
    if params is not None and hasattr(params, "get"):
        try:
            pval = params.get(param_key)
            if pval is not None:
                return int(getattr(pval, "value", pval))
        except Exception:
            return None
    return None


def _resolve_price(spec: Any, view: Any) -> Decimal | None:
    if spec.type == OrderType.LIMIT and spec.price is not None:
        return Decimal(str(spec.price))
    quote = view.quote_for(spec.instrument) if hasattr(view, "quote_for") else None
    if quote is not None:
        return Decimal(str(quote.ltp))
    return None


class CashAvailableRule:
    """Ensure cash after order remains at or above margin buffer percentage of capital."""

    name = "cash_available"

    def check(self, intent: Intent, view: Any) -> RiskDecision:
        if not isinstance(intent, PlaceOrder):
            return RiskDecision(allow=True, rule=self.name, reason="Not a placement", values={})

        spec = intent.spec
        buffer_pct = _get_float_param(view, "margin_buffer_pct")
        if buffer_pct is None:
            return RiskDecision(
                allow=False, rule=self.name, reason="Missing margin_buffer_pct param", values={}
            )

        capital: Decimal = getattr(view, "allocated_capital", Decimal("100000.00"))
        min_cash = (Decimal(str(buffer_pct)) / Decimal("100")) * capital

        price = _resolve_price(spec, view)
        if price is None or price <= Decimal("0.00"):
            return RiskDecision(
                allow=False,
                rule=self.name,
                reason="Cannot resolve order execution price",
                values={},
            )

        portfolio = getattr(view, "portfolio", None)
        cur_cash: Decimal = getattr(portfolio, "cash", capital) if portfolio else capital

        order_cost = Decimal(spec.qty) * price if spec.side == Side.BUY else Decimal("0.00")
        cash_after = cur_cash - order_cost

        vals = {
            "cash_after": cash_after,
            "min_cash_required": min_cash,
            "margin_buffer_pct": buffer_pct,
        }

        if cash_after < min_cash:
            reason = (
                f"Cash after order ₹{cash_after} falls below required buffer "
                f"₹{min_cash} ({buffer_pct}%)"
            )
            return RiskDecision(
                allow=False,
                rule=self.name,
                reason=reason,
                values=vals,
            )

        return RiskDecision(
            allow=True, rule=self.name, reason="Sufficient cash available", values=vals
        )


class MaxOrderValueRule:
    """Fat-finger order size safeguard."""

    name = "max_order_value_pct"

    def check(self, intent: Intent, view: Any) -> RiskDecision:
        if not isinstance(intent, PlaceOrder):
            return RiskDecision(allow=True, rule=self.name, reason="Not a placement", values={})

        spec = intent.spec
        max_pct = _get_float_param(view, "max_order_value_pct")
        if max_pct is None:
            return RiskDecision(
                allow=False, rule=self.name, reason="Missing max_order_value_pct param", values={}
            )

        capital: Decimal = getattr(view, "allocated_capital", Decimal("100000.00"))
        max_val = (Decimal(str(max_pct)) / Decimal("100")) * capital

        price = _resolve_price(spec, view)
        if price is None:
            return RiskDecision(
                allow=False, rule=self.name, reason="Cannot resolve order price", values={}
            )

        order_val = Decimal(spec.qty) * price
        vals = {"order_value": order_val, "max_order_value": max_val, "limit_pct": max_pct}

        if order_val > max_val:
            return RiskDecision(
                allow=False,
                rule=self.name,
                reason=f"Order value ₹{order_val} exceeds limit ₹{max_val} ({max_pct}%)",
                values=vals,
            )

        return RiskDecision(
            allow=True, rule=self.name, reason="Order value within limit", values=vals
        )


class MaxPositionRule:
    """Restricts maximum single position size relative to portfolio capital."""

    name = "max_position_pct"

    def check(self, intent: Intent, view: Any) -> RiskDecision:
        if not isinstance(intent, PlaceOrder):
            return RiskDecision(allow=True, rule=self.name, reason="Not a placement", values={})

        spec = intent.spec
        max_pct = _get_float_param(view, "max_position_pct")
        if max_pct is None:
            return RiskDecision(
                allow=False, rule=self.name, reason="Missing max_position_pct param", values={}
            )

        capital: Decimal = getattr(view, "allocated_capital", Decimal("100000.00"))
        max_pos_val = (Decimal(str(max_pct)) / Decimal("100")) * capital

        price = _resolve_price(spec, view)
        if price is None:
            return RiskDecision(
                allow=False, rule=self.name, reason="Cannot resolve price", values={}
            )

        # Calculate position qty after order
        cur_qty = 0
        positions = getattr(view, "positions", {})
        for pos in positions.values():
            if str(pos.instrument) == str(spec.instrument):
                cur_qty = pos.qty
                break

        signed_qty = spec.qty if spec.side == Side.BUY else -spec.qty
        new_qty = cur_qty + signed_qty
        new_pos_val = abs(Decimal(new_qty)) * price

        vals = {
            "position_value": new_pos_val,
            "max_position_value": max_pos_val,
            "limit_pct": max_pct,
        }

        if new_pos_val > max_pos_val:
            reason = (
                f"Projected position value ₹{new_pos_val} exceeds limit ₹{max_pos_val} ({max_pct}%)"
            )
            return RiskDecision(
                allow=False,
                rule=self.name,
                reason=reason,
                values=vals,
            )

        return RiskDecision(
            allow=True, rule=self.name, reason="Position size within limit", values=vals
        )


class MaxOpenPositionsRule:
    """Caps concurrent open positions."""

    name = "max_open_positions"

    def check(self, intent: Intent, view: Any) -> RiskDecision:
        if not isinstance(intent, PlaceOrder):
            return RiskDecision(allow=True, rule=self.name, reason="Not a placement", values={})

        max_open = _get_int_param(view, "max_open_positions")
        if max_open is None:
            return RiskDecision(
                allow=False, rule=self.name, reason="Missing max_open_positions param", values={}
            )

        positions = getattr(view, "positions", {})
        open_count = sum(1 for p in positions.values() if p.qty != 0)

        # Check if intent opens a brand new position
        spec = intent.spec
        is_new_instrument = True
        for p in positions.values():
            if str(p.instrument) == str(spec.instrument) and p.qty != 0:
                is_new_instrument = False
                break

        projected_count = open_count + 1 if is_new_instrument else open_count
        vals = {"open_positions": projected_count, "max_open_positions": max_open}

        if projected_count > max_open:
            return RiskDecision(
                allow=False,
                rule=self.name,
                reason=f"Projected open positions {projected_count} exceeds limit {max_open}",
                values=vals,
            )

        return RiskDecision(
            allow=True, rule=self.name, reason="Open positions within limit", values=vals
        )


class MaxGrossExposureRule:
    """Restricts total gross exposure as a percentage of capital."""

    name = "max_gross_exposure_pct"

    def check(self, intent: Intent, view: Any) -> RiskDecision:
        if not isinstance(intent, PlaceOrder):
            return RiskDecision(allow=True, rule=self.name, reason="Not a placement", values={})

        spec = intent.spec
        max_pct = _get_float_param(view, "max_gross_exposure_pct")
        if max_pct is None:
            return RiskDecision(
                allow=False,
                rule=self.name,
                reason="Missing max_gross_exposure_pct param",
                values={},
            )

        capital: Decimal = getattr(view, "allocated_capital", Decimal("100000.00"))
        max_exp = (Decimal(str(max_pct)) / Decimal("100")) * capital

        order_price = _resolve_price(spec, view)
        if order_price is None:
            return RiskDecision(
                allow=False, rule=self.name, reason="Cannot resolve price", values={}
            )

        # Calculate current exposure across all open positions
        cur_exposure = Decimal("0.00")
        positions = getattr(view, "positions", {})
        existing_matched_qty = 0

        for pos in positions.values():
            if pos.qty == 0:
                continue
            q = view.quote_for(pos.instrument) if hasattr(view, "quote_for") else None
            p_price = Decimal(str(q.ltp)) if q is not None else pos.avg_price
            if str(pos.instrument) == str(spec.instrument):
                existing_matched_qty = pos.qty
            else:
                cur_exposure += abs(Decimal(pos.qty)) * p_price

        signed_qty = spec.qty if spec.side == Side.BUY else -spec.qty
        projected_qty = existing_matched_qty + signed_qty
        projected_exposure = cur_exposure + (abs(Decimal(projected_qty)) * order_price)

        vals = {
            "gross_exposure": projected_exposure,
            "max_gross_exposure": max_exp,
            "limit_pct": max_pct,
        }

        if projected_exposure > max_exp:
            reason = f"Gross exposure ₹{projected_exposure} exceeds limit ₹{max_exp} ({max_pct}%)"
            return RiskDecision(
                allow=False,
                rule=self.name,
                reason=reason,
                values=vals,
            )

        return RiskDecision(
            allow=True, rule=self.name, reason="Gross exposure within limit", values=vals
        )


class PriceDeviationRule:
    """Limit order price deviation safeguard against stale books."""

    name = "price_deviation_pct"

    def check(self, intent: Intent, view: Any) -> RiskDecision:
        if not isinstance(intent, PlaceOrder):
            return RiskDecision(allow=True, rule=self.name, reason="Not a placement", values={})

        spec = intent.spec
        if spec.type != OrderType.LIMIT or spec.price is None:
            return RiskDecision(allow=True, rule=self.name, reason="Not a limit order", values={})

        max_pct = _get_float_param(view, "price_deviation_pct")
        if max_pct is None:
            return RiskDecision(
                allow=False, rule=self.name, reason="Missing price_deviation_pct param", values={}
            )

        quote = view.quote_for(spec.instrument) if hasattr(view, "quote_for") else None
        if quote is None or quote.ltp <= 0.0:
            return RiskDecision(
                allow=False,
                rule=self.name,
                reason="Missing quote LTP for price deviation",
                values={},
            )

        ltp_dec = Decimal(str(quote.ltp))
        limit_dec = spec.price
        deviation_pct = (abs(limit_dec - ltp_dec) / ltp_dec) * Decimal("100")

        vals = {
            "deviation_pct": deviation_pct,
            "max_deviation_pct": max_pct,
            "ltp": ltp_dec,
            "limit_price": limit_dec,
        }

        if deviation_pct > Decimal(str(max_pct)):
            return RiskDecision(
                allow=False,
                rule=self.name,
                reason=f"Limit price deviation {deviation_pct:.2f}% exceeds limit {max_pct}%",
                values=vals,
            )

        return RiskDecision(
            allow=True, rule=self.name, reason="Price deviation within limit", values=vals
        )


class MaxOrdersPerMinuteRule:
    """Order rate throttle per minute below venue and broker rate limits."""

    name = "max_orders_per_minute"

    def check(self, intent: Intent, view: Any) -> RiskDecision:
        if not isinstance(intent, PlaceOrder):
            return RiskDecision(allow=True, rule=self.name, reason="Not a placement", values={})

        max_orders = _get_int_param(view, "max_orders_per_minute")
        if max_orders is None:
            return RiskDecision(
                allow=False, rule=self.name, reason="Missing max_orders_per_minute param", values={}
            )

        orders_count = getattr(view, "orders_last_minute", 0)
        vals = {"orders_last_minute": orders_count, "max_orders_per_minute": max_orders}

        if orders_count >= max_orders:
            return RiskDecision(
                allow=False,
                rule=self.name,
                reason=f"Order rate {orders_count}/min reached limit {max_orders}/min",
                values=vals,
            )

        return RiskDecision(
            allow=True, rule=self.name, reason="Order frequency within limit", values=vals
        )


class MaxDailyTradesRule:
    """Overtrading guard capping maximum completed trades in a day."""

    name = "max_daily_trades"

    def check(self, intent: Intent, view: Any) -> RiskDecision:
        if not isinstance(intent, PlaceOrder):
            return RiskDecision(allow=True, rule=self.name, reason="Not a placement", values={})

        max_trades = _get_int_param(view, "max_daily_trades")
        if max_trades is None:
            return RiskDecision(
                allow=False, rule=self.name, reason="Missing max_daily_trades param", values={}
            )

        daily_trades = getattr(view, "daily_trades_count", 0)
        vals = {"daily_trades_count": daily_trades, "max_daily_trades": max_trades}

        if daily_trades >= max_trades:
            return RiskDecision(
                allow=False,
                rule=self.name,
                reason=f"Daily trades {daily_trades} reached limit {max_trades}",
                values=vals,
            )

        return RiskDecision(
            allow=True, rule=self.name, reason="Daily trades within limit", values=vals
        )


class DailyLossLimitRule:
    """Daily realized and marked loss ceiling."""

    name = "daily_loss_limit_pct"

    def check(self, intent: Intent, view: Any) -> RiskDecision:
        if not isinstance(intent, PlaceOrder):
            return RiskDecision(allow=True, rule=self.name, reason="Not a placement", values={})

        limit_pct = _get_float_param(view, "daily_loss_limit_pct")
        if limit_pct is None:
            return RiskDecision(
                allow=False, rule=self.name, reason="Missing daily_loss_limit_pct param", values={}
            )

        capital: Decimal = getattr(view, "allocated_capital", Decimal("100000.00"))
        max_loss = (Decimal(str(limit_pct)) / Decimal("100")) * capital

        daily_pnl: Decimal = getattr(view, "daily_pnl", Decimal("0.00"))
        loss = abs(daily_pnl) if daily_pnl < Decimal("0.00") else Decimal("0.00")

        vals = {"daily_loss": loss, "max_daily_loss": max_loss, "limit_pct": limit_pct}

        if loss >= max_loss:
            return RiskDecision(
                allow=False,
                rule=self.name,
                reason=f"Daily loss ₹{loss} reached or breached limit ₹{max_loss} ({limit_pct}%)",
                values=vals,
            )

        return RiskDecision(
            allow=True, rule=self.name, reason="Daily loss within limit", values=vals
        )
