"""Fee schedule and calculation errors."""

from __future__ import annotations

from datetime import date

from algodesk.api.errors import AlgoDeskError


class FeeScheduleError(AlgoDeskError):
    """Raised when fee calculation encounters an unverified, missing, or out-of-bounds component."""

    def __init__(
        self,
        what: str,
        *,
        where: str = "FeeEngine",
        fix: str = "Verify fee schedule dates or configure fees.fallback = 'earliest'",
        trade_date: date | None = None,
        component: str | None = None,
        earliest_verified: date | None = None,
    ) -> None:
        super().__init__(what=what, where=where, fix=fix)
        self.trade_date = trade_date
        self.component = component
        self.earliest_verified = earliest_verified
