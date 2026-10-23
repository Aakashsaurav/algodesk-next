"""Pydantic models for fee schedules and charge components."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _to_decimal(v: Any) -> Decimal | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


ComponentKind = Literal["flat", "pct", "pct_capped", "per_crore", "per_scrip_day"]
ComponentSide = Literal["buy", "sell", "both"]


class ScheduleMeta(BaseModel):
    """Metadata describing a fee schedule source and verification status."""

    model_config = ConfigDict(frozen=True, extra="allow")

    name: str
    source: str
    verified_on: date | str | None = None
    verified: bool = True


class ScheduleComponent(BaseModel):
    """An individual fee component definition."""

    model_config = ConfigDict(frozen=True, extra="allow")

    name: str
    kind: ComponentKind
    rate: Decimal | None = None
    amount: Decimal | None = None
    cap: Decimal | None = None
    minimum: Decimal | None = None
    segment: str
    product: str
    side: ComponentSide = "both"
    effective_from: date
    effective_to: date | None = None
    verified: bool = True

    @field_validator("rate", "amount", "cap", "minimum", mode="before")
    @classmethod
    def _coerce_decimal(cls, v: Any) -> Decimal | None:
        return _to_decimal(v)


class ScheduleTax(BaseModel):
    """Tax specification applying to named fee components."""

    model_config = ConfigDict(frozen=True, extra="allow")

    name: str
    rate: Decimal
    applies_to: list[str] = Field(default_factory=list)
    effective_from: date | None = None
    effective_to: date | None = None

    @field_validator("rate", mode="before")
    @classmethod
    def _coerce_rate(cls, v: Any) -> Decimal:
        dec = _to_decimal(v)
        if dec is None:
            raise ValueError("Tax rate cannot be None")
        return dec


class Schedule(BaseModel):
    """Complete fee schedule including components and taxes."""

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="allow")

    meta: ScheduleMeta
    components: list[ScheduleComponent] = Field(default_factory=list, alias="component")
    taxes: list[ScheduleTax] = Field(default_factory=list, alias="tax")
