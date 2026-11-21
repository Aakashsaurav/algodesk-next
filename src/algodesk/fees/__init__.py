"""AlgoDesk fee engine and schedule models."""

from __future__ import annotations

from algodesk.fees.engine import ChargesList, FeeEngine, charges_for_fills
from algodesk.fees.errors import FeeScheduleError
from algodesk.fees.mapping import resolve_fee_segment_and_product
from algodesk.fees.models import (
    ComponentKind,
    ComponentSide,
    Schedule,
    ScheduleComponent,
    ScheduleMeta,
    ScheduleTax,
)

__all__ = [
    "ChargesList",
    "ComponentKind",
    "ComponentSide",
    "FeeEngine",
    "FeeScheduleError",
    "Schedule",
    "ScheduleComponent",
    "ScheduleMeta",
    "ScheduleTax",
    "charges_for_fills",
    "resolve_fee_segment_and_product",
]
