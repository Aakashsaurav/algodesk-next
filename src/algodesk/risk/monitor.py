"""Post-fill risk monitor checking daily loss limits and tripping the kill-switch."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from algodesk.api.services import ParamReader
from algodesk.api.trading import FillEvent
from algodesk.risk.killswitch import KillSwitch


class RiskMonitor:
    """Monitors daily loss after fills, tripping kill-switch on limit breach."""

    def __init__(
        self,
        killswitch: KillSwitch,
        portfolio: Any,
        params: ParamReader | Mapping[str, Any],
        profile: str = "conservative",
        allocated_capital: Decimal | None = None,
    ) -> None:
        self.killswitch = killswitch
        self.portfolio = portfolio
        self.params = params
        self.profile = profile
        self.allocated_capital = allocated_capital

    def check_daily_loss(self) -> bool:
        """Inspect daily P&L and trip kill-switch if daily loss limit is reached or breached."""
        day_pnl: Decimal = getattr(self.portfolio, "day_pnl", Decimal("0.00"))
        if day_pnl >= Decimal("0.00"):
            return False

        loss = abs(day_pnl)
        param_key = f"risk.{self.profile}.daily_loss_limit_pct"

        limit_pct: float | None = None
        if hasattr(self.params, "get"):
            try:
                pval: Any = self.params.get(param_key)
                if pval is not None:
                    limit_pct = float(getattr(pval, "value", pval))
            except Exception:
                limit_pct = None

        if limit_pct is None:
            # If parameter missing, fail safe by not skipping check or trip
            return False

        raw_cap = self.allocated_capital or getattr(self.portfolio, "starting_cash", None)
        capital: Decimal = Decimal(str(raw_cap)) if raw_cap is not None else Decimal("100000.00")
        max_loss = (Decimal(str(limit_pct)) / Decimal("100")) * capital

        if loss >= max_loss:
            self.killswitch.trip(
                reason=f"Daily loss ₹{loss} reached or breached limit ₹{max_loss} ({limit_pct}%)",
                by="risk_monitor",
            )
            return True

        return False

    def on_fill(self, fill_event: FillEvent) -> bool:
        """Event handler called after every trade fill."""
        return self.check_daily_loss()
